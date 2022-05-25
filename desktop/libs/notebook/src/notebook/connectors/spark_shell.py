#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from builtins import range, object
import logging
import re
import sys
import time
import textwrap
import json

from desktop.conf import USE_DEFAULT_CONFIGURATION
from desktop.lib.exceptions_renderable import PopupException
from desktop.lib.i18n import force_unicode
from desktop.lib.rest.http_client import RestException
from desktop.models import DefaultConfiguration
from desktop.auth.backend import rewrite_user

from notebook.data_export import download as spark_download
from notebook.connectors.base import Api, QueryError, SessionExpired, _get_snippet_session

if sys.version_info[0] > 2:
  from django.utils.translation import gettext as _
else:
  from django.utils.translation import ugettext as _


LOG = logging.getLogger(__name__)


try:
  from spark.conf import LIVY_SERVER_SESSION_KIND
  from spark.livy_client import get_api as get_spark_api
except ImportError as e:
  LOG.exception('Spark is not enabled')

SESSION_KEY = '%(username)s-%(interpreter_name)s'

class SparkApi(Api):

  SPARK_UI_RE = re.compile("Started SparkUI at (http[s]?://([0-9a-zA-Z-_\.]+):(\d+))")
  YARN_JOB_RE = re.compile("tracking URL: (http[s]?://.+/)")
  STANDALONE_JOB_RE = re.compile("Got job (\d+)")


  def __init__(self, user, interpreter):
    super(SparkApi, self).__init__(user=user, interpreter=interpreter)


  def get_api(self):
    return get_spark_api(self.user, self.interpreter)


  @staticmethod
  def get_livy_props(lang, properties=None):
    props = dict([(p['name'], p['value']) for p in SparkConfiguration.PROPERTIES])
    if properties is not None:
      props.update(dict([(p['name'], p['value']) for p in properties if 'name' in p.keys() and 'value' in p.keys()]))

    # HUE-4761: Hue's session request is causing Livy to fail with "JsonMappingException: Can not deserialize
    # instance of scala.collection.immutable.List out of VALUE_STRING token" due to List type values
    # not being formed properly, they are quoted csv strings (without brackets) instead of proper List
    # types, this is for keys; archives, jars, files and pyFiles. The Mako frontend probably should be
    # modified to pass the values as Livy expects but for now we coerce these types to be Lists.
    # Issue only occurs when non-default values are used because the default path properly sets the
    # empty list '[]' for these four values.
    # Note also that Livy has a 90 second timeout for the session request to complete, this needs to
    # be increased for requests that take longer, for example when loading large archives.
    for key in ['archives', 'jars', 'files', 'pyFiles']:
      if key not in props:
        continue
      if type(props[key]) is list:
        continue
      LOG.debug("Check List type: {} was not a list".format(key))
      _tmp = props[key]
      props[key] = _tmp.split(",")

    # Convert the conf list to a dict for Livy
    LOG.debug("Property Spark Conf kvp list from UI is: " + str(props['conf']))
    props['conf'] = {conf.get('key'): conf.get('value') for i, conf in enumerate(props['conf'])}
    LOG.debug("Property Spark Conf dictionary is: " + str(props['conf']))

    props['kind'] = 'sql' if lang == 'sparksql' else lang

    return props


  @staticmethod
  def to_properties(props=None):
    properties = list()
    for p in SparkConfiguration.PROPERTIES:
      properties.append(p.copy())

    if props is not None:
      for p in properties:
        if p['name'] in props:
          p['value'] = props[p['name']]

    return properties


  def _get_session_key(self):
    return SESSION_KEY % {
      'username': self.user.username if hasattr(self.user, 'username') else self.user,
      'interpreter_name': self.interpreter['name']
    }


  def _check_session(self, session):
    '''
    Check if the session is actually present and its state is healthy.
    '''
    api = self.get_api()
    try:
      session_present = api.get_session(session['id'])
    except Exception as e:
      session_present = None

    if session_present and session_present['state'] not in ('dead', 'shutting_down', 'error', 'killed'):
      return session_present


  def create_session(self, lang='scala', properties=None):
    api = self.get_api()
    stored_session_info = self._get_session_info_from_user()

    if stored_session_info:
      session_present = self._check_session(stored_session_info)
      if session_present:
        return stored_session_info

    if not properties and USE_DEFAULT_CONFIGURATION.get():
      user_config = DefaultConfiguration.objects.get_configuration_for_user(app='spark', user=self.user)
      if user_config is not None:
        properties = user_config.properties_list

    props = self.get_livy_props(lang, properties)

    response = api.create_session(**props)

    status = api.get_session(response['id'])
    count = 0

    while status['state'] == 'starting' and count < 120:
      status = api.get_session(response['id'])
      count += 1
      time.sleep(1)

    if status['state'] != 'idle':
      info = '\n'.join(status['log']) if status['log'] else 'timeout'
      raise QueryError(_('The Spark session is %s and could not be created in the cluster: %s') % (status['state'], info))

    new_session_info = {
        'type': lang,
        'id': response['id'],
        'properties': self.to_properties(props)
    }
    self._set_session_info_to_user(new_session_info)

    return new_session_info
    

  def execute(self, notebook, snippet):
    api = self.get_api()
    session = _get_snippet_session(notebook, snippet)

    response = self._execute(api, session, snippet.get('type'), snippet['statement'])
    return response


  def _execute(self, api, session, snippet_type, statement):

    if not session or not self._check_session(session):
      stored_session_info = self._get_session_info_from_user()
      if stored_session_info and self._check_session(stored_session_info):
        session = stored_session_info
      else:
        session = self.create_session(snippet_type)

    try:
      response = api.submit_statement(session['id'], statement)
      return {
          'id': response['id'],
          'has_result_set': True,
          'sync': False
      }
    except Exception as e:
      message = force_unicode(str(e)).lower()
      if re.search("session ('\d+' )?not found", message) or 'connection refused' in message or 'session is in state busy' in message:
        raise SessionExpired(e)
      else:
        raise e


  def check_status(self, notebook, snippet):
    api = self.get_api()
    session = _get_snippet_session(notebook, snippet)
    cell = snippet['result']['handle']['id']

    session = self._handle_session_health_check(session)

    try:
      response = api.fetch_data(session['id'], cell)
      return {
          'status': response['state'],
      }
    except Exception as e:
      message = force_unicode(str(e)).lower()
      if re.search("session ('\d+' )?not found", message):
        raise SessionExpired(e)
      else:
        raise e


  def fetch_result(self, notebook, snippet, rows, start_over):
    api = self.get_api()
    session = _get_snippet_session(notebook, snippet)
    cell = snippet['result']['handle']['id']

    session = self._handle_session_health_check(session)

    response = self._fetch_result(api, session, cell, start_over)

    # Close unused sessions if there are any.
    # Clean here since /fetch_result_data is called only once after the /execute call
    if self._get_session_info_from_user():
      self._close_unused_sessions()

    return response


  def _fetch_result(self, api, session, cell, start_over):
    try:
      response = api.fetch_data(session['id'], cell)
    except Exception as e:
      message = force_unicode(str(e)).lower()
      if re.search("session ('\d+' )?not found", message):
        raise SessionExpired(e)
      else:
        raise e

    content = response['output']

    if content['status'] == 'ok':
      data = content['data']
      images = []

      try:
        table = data['application/vnd.livy.table.v1+json']
      except KeyError:
        try:
          images = [data['image/png']]
        except KeyError:
          images = []
        if 'application/json' in data:
          result = data['application/json']
          data = result['data']
          meta = [{'name': field['name'], 'type': field['type'], 'comment': ''} for field in result['schema']['fields']]
          type = 'table'
        else:
          data = [[data['text/plain']]]
          meta = [{'name': 'Header', 'type': 'STRING_TYPE', 'comment': ''}]
          type = 'text'
      else:
        data = table['data']
        headers = table['headers']
        meta = [{'name': h['name'], 'type': h['type'], 'comment': ''} for h in headers]
        type = 'table'

      # Non start_over not supported
      if not start_over:
        data = []

      return {
          'data': data,
          'images': images,
          'meta': meta,
          'type': type
      }
    elif content['status'] == 'error':
      tb = content.get('traceback', None)

      if tb is None or not tb:
        msg = content.get('ename', 'unknown error')

        evalue = content.get('evalue')
        if evalue is not None:
          msg = '%s: %s' % (msg, evalue)
      else:
        msg = ''.join(tb)

      raise QueryError(msg)


  def cancel(self, notebook, snippet):
    api = self.get_api()
    session = _get_snippet_session(notebook, snippet)

    session = self._handle_session_health_check(session)

    try:
      response = api.cancel(session['id'])
    except Exception as e:
      message = force_unicode(str(e)).lower()
      LOG.debug(message)

    return {'status': 0}


  def get_log(self, notebook, snippet, startFrom=0, size=None):
    response = {'status': 0}
    api = self.get_api()
    session = _get_snippet_session(notebook, snippet)

    session = self._handle_session_health_check(session)
    try:
      response = api.get_log(session['id'], startFrom=startFrom, size=size)
    except RestException as e:
      message = force_unicode(str(e)).lower()
      LOG.debug(message)

    return response
  

  def _handle_session_health_check(self, session):

    if not session or not self._check_session(session):
      stored_session_info = self._get_session_info_from_user()
      if stored_session_info and self._check_session(stored_session_info):
        session = stored_session_info
      else:
        raise PopupException(_("Session expired. Please create new session and try again."))
    
    return session


  def close_statement(self, notebook, snippet): # Individual statements cannot be closed
    pass


  def close_session(self, session):
    api = self.get_api()

    if session['id'] is not None:
      try:
        api.close(session['id'])
        return {
          'session': session['id'],
          'status': 0
        }
      except RestException as e:
        if e.code == 404 or e.code == 500: # TODO remove the 500
          raise SessionExpired(e)
      finally:
        stored_session_info = self._get_session_info_from_user()
        if stored_session_info and session['id'] == stored_session_info['id']:
          self._remove_session_info_from_user()
    else:
      return {'status': -1}


  def get_jobs(self, notebook, snippet, logs):
    if self._is_yarn_mode():
      # Tracking URL is found at the start of the logs
      start_logs = self.get_log(notebook, snippet, startFrom=0, size=100)
      return self._get_yarn_jobs(start_logs)
    else:
      return self._get_standalone_jobs(logs)


  def autocomplete(self, snippet, database=None, table=None, column=None, nested=None, operation=None):
    response = {}
    # As booting a new SQL session is slow and we don't send the id of the current one in /autocomplete
    # we could implement this by introducing an API cache per user similarly to SqlAlchemy.
    api = self.get_api()

    # Trying to close unused sessions if there are any.
    # Calling the method here since this /autocomplete call can be frequent enough and we dont need dedicated one.
    if self._get_session_info_from_user():
      self._close_unused_sessions()
    
    stored_session_info = self._get_session_info_from_user()
    if stored_session_info and self._check_session(stored_session_info):
      session = stored_session_info
    else:
      session = self.create_session(snippet.get('type'))

    if database is None:
      response['databases'] = self._show_databases(api, session, snippet.get('type'))
    elif table is None:
      response['tables_meta'] = self._show_tables(api, session, snippet.get('type'), database)
    elif column is None:
      columns = self._get_columns(api, session, snippet.get('type'), database, table)
      response['columns'] = [col['name'] for col in columns]
      response['extended_columns'] = [{
          'comment': col.get('comment'),
          'name': col.get('name'),
          'type': col['type']
        }
        for col in columns
      ]

    return response


  def _close_unused_sessions(self):
    '''
    Closes all unused Livy sessions for a particular user to free up session resources.
    '''
    api = self.get_api()
    all_sessions = {}

    try:
      all_sessions = api.get_sessions()
    except Exception as e:
      message = force_unicode(str(e)).lower()
      LOG.debug(message)

    if all_sessions:
      stored_session_info = self._get_session_info_from_user()
      for session in all_sessions['sessions']:
        if session['owner'] == self.user.username and session['id'] != stored_session_info['id'] and \
          session['state'] in ('idle', 'shutting_down', 'error', 'dead', 'killed'):
          self.close_session(session)


  def _check_status_and_fetch_result(self, api, session, execute_resp):
    check_status = api.fetch_data(session['id'], execute_resp['id'])

    count = 0
    while check_status['state'] in ['running', 'waiting'] and count < 120:
      check_status = api.fetch_data(session['id'], execute_resp['id'])
      count += 1
      time.sleep(1)

    if check_status['state'] == 'available':
      return self._fetch_result(api, session, execute_resp['id'], start_over=True)


  def _show_databases(self, api, session, snippet_type):
    show_db_execute = self._execute(api, session, snippet_type, 'SHOW DATABASES')
    db_list = self._check_status_and_fetch_result(api, session, show_db_execute)

    if db_list:
      return [db[0] for db in db_list['data']]


  def _show_tables(self, api, session, snippet_type, database):
    use_db_execute = self._execute(api, session, snippet_type, 'USE %(database)s' % {'database': database})
    use_db_resp = self._check_status_and_fetch_result(api, session, use_db_execute)

    show_tables_execute = self._execute(api, session, snippet_type, 'SHOW TABLES')
    tables_list = self._check_status_and_fetch_result(api, session, show_tables_execute)

    if tables_list:
      return [table[1] for table in tables_list['data']]


  def _get_columns(self, api, session, snippet_type, database, table):
    use_db_execute = self._execute(api, session, snippet_type, 'USE %(database)s' % {'database': database})
    use_db_resp = self._check_status_and_fetch_result(api, session, use_db_execute)

    describe_tables_execute = self._execute(api, session, snippet_type, 'DESCRIBE %(table)s' % {'table': table})
    columns_list = self._check_status_and_fetch_result(api, session, describe_tables_execute)

    if columns_list:
      return [{
        'name': col[0],
        'type': col[1],
        'comment': '',
      } for col in columns_list['data']]


  def get_sample_data(self, snippet, database=None, table=None, column=None, is_async=False, operation=None):
    api = self.get_api()

    # Trying to close unused sessions if there are any.
    # Calling the method here since this /sample_data call can be frequent enough and we dont need dedicated one.
    if self._get_session_info_from_user():
      self._close_unused_sessions()

    stored_session_info = self._get_session_info_from_user()
    if stored_session_info and self._check_session(stored_session_info):
      session = stored_session_info
    else:
      session = self.create_session(snippet.get('type'))

    statement = self._get_select_query(database, table, column, operation)

    sample_execute = self._execute(api, session, snippet.get('type'), statement)
    sample_result = self._check_status_and_fetch_result(api, session, sample_execute)

    response = {
      'status': 0,
      'result': {}
    }
    response['rows'] = sample_result['data']
    response['full_headers'] = sample_result['meta']

    return response


  def get_browse_query(self, snippet, database, table, partition_spec=None):
    return self._get_select_query(database, table)

  
  def _get_select_query(self, database, table, column=None, operation=None, limit=100):
    if operation == 'hello':
      statement = "SELECT 'Hello World!'"
    else:
      column = '%(column)s' % {'column': column} if column else '*'
      statement = textwrap.dedent('''\
          SELECT %(column)s
          FROM %(database)s.%(table)s
          LIMIT %(limit)s
          ''' % {
            'database': database,
            'table': table,
            'column': column,
            'limit': limit,
        })

    return statement


  def _get_standalone_jobs(self, logs):
    job_ids = set([])

    # Attempt to find Spark UI Host and Port from startup logs
    spark_ui_url = self.SPARK_UI_RE.search(logs)

    if not spark_ui_url:
      LOG.warning('Could not find the Spark UI URL in the session logs.')
      return []
    else:
      spark_ui_url = spark_ui_url.group(1)

    # Standalone/Local mode runs on same host as Livy, attempt to find Job IDs in Spark log
    for match in self.STANDALONE_JOB_RE.finditer(logs):
      job_id = match.group(1)
      job_ids.add(job_id)

    jobs = [{
      'name': job_id,
      'url': '%s/jobs/job/?id=%s' % (spark_ui_url, job_id)
    } for job_id in job_ids]

    return jobs


  def _get_yarn_jobs(self, logs):
    tracking_urls = set([])

    # YARN mode only outputs the tracking-proxy URL, not Job IDs
    for match in self.YARN_JOB_RE.finditer(logs):
      url = match.group(1)
      tracking_urls.add(url)

    jobs = [{
      'name': url.strip('/').split('/')[-1],  # application_id is the last token
      'url': url
    } for url in tracking_urls]

    return jobs


  def _is_yarn_mode(self):
    return LIVY_SERVER_SESSION_KIND.get() == "yarn"


  def _get_session_info_from_user(self):
    self.user = rewrite_user(self.user)
    session_key = self._get_session_key()

    if self.user.profile.data.get(session_key):
      return self.user.profile.data[session_key]


  def _set_session_info_to_user(self, session_info):
    self.user = rewrite_user(self.user)
    session_key = self._get_session_key()

    self.user.profile.update_data({session_key: session_info})
    self.user.profile.save()


  def _remove_session_info_from_user(self):
    self.user = rewrite_user(self.user)
    session_key = self._get_session_key()

    if self.user.profile.data.get(session_key):
      json_data = self.user.profile.data
      json_data.pop(session_key)
      self.user.profile.json_data = json.dumps(json_data)
    
    self.user.profile.save()


class SparkConfiguration(object):

  APP_NAME = 'spark'

  PROPERTIES = [
    {
      "name": "conf",
      "nice_name": _("Spark Conf"),
      "help_text": _("Add one or more Spark conf properties to the session."),
      "type": "settings",
      "is_yarn": False,
      "multiple": True,
      "defaultValue": [],
      "value": [],
    },
    {
      "name": "jars",
      "nice_name": _("Jars"),
      "help_text": _("Add one or more JAR files to the list of resources."),
      "type": "csv-hdfs-files",
      "is_yarn": False,
      "multiple": True,
      "defaultValue": [],
      "value": [],
    }, {
      "name": "files",
      "nice_name": _("Files"),
      "help_text": _("Files to be placed in the working directory of each executor."),
      "type": "csv-hdfs-files",
      "is_yarn": False,
      "multiple": True,
      "defaultValue": [],
      "value": [],
    }, {
      "name": "pyFiles",
      "nice_name": _("pyFiles"),
      "help_text": _("Python files to be placed in the working directory of each executor."),
      "type": "csv-hdfs-files",
      "is_yarn": False,
      "multiple": True,
      "defaultValue": [],
      "value": [],
    }, {
      "name": "driverMemory",
      "nice_name": _("Driver Memory"),
      "help_text": _("Amount of memory to use for the driver process in GB. (Default: 1). "),
      "type": "jvm",
      "is_yarn": False,
      "multiple": False,
      "defaultValue": '1G',
      "value": '1G',
    },
    # YARN-only properties
    {
      "name": "driverCores",
      "nice_name": _("Driver Cores"),
      "help_text": _("Number of cores used by the driver, only in cluster mode (Default: 1)"),
      "type": "number",
      "is_yarn": True,
      "multiple": False,
      "defaultValue": 1,
      "value": 1,
    }, {
      "name": "executorMemory",
      "nice_name": _("Executor Memory"),
      "help_text": _("Amount of memory to use per executor process in GB. (Default: 1)"),
      "type": "jvm",
      "is_yarn": True,
      "multiple": False,
      "defaultValue": '1G',
      "value": '1G',
    }, {
      "name": "executorCores",
      "nice_name": _("Executor Cores"),
      "help_text": _("Number of cores used by the driver, only in cluster mode (Default: 1)"),
      "type": "number",
      "is_yarn": True,
      "multiple": False,
      "defaultValue": 1,
      "value": 1,
    }, {
      "name": "queue",
      "nice_name": _("Queue"),
      "help_text": _("The YARN queue to submit to, only in cluster mode (Default: default)"),
      "type": "string",
      "is_yarn": True,
      "multiple": False,
      "defaultValue": 'default',
      "value": 'default',
    }, {
      "name": "archives",
      "nice_name": _("Archives"),
      "help_text": _("Archives to be extracted into the working directory of each executor, only in cluster mode."),
      "type": "csv-hdfs-files",
      "is_yarn": True,
      "multiple": True,
      "defaultValue": [],
      "value": [],
    }
  ]
