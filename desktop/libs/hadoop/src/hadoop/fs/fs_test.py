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

import logging
import pytest
import os
import stat
import tempfile
import unittest

from django.test import TestCase
from hadoop import fs, pseudo_hdfs4


logger = logging.getLogger()


class LocalSubFileSystemTest(TestCase):
  def setup_method(self, method):
    self.root = tempfile.mkdtemp()
    self.fs = fs.LocalSubFileSystem(self.root)

  def teardown_method(self, method):
    if not os.listdir(self.root):
      os.rmdir(self.root)
    else:
      logger.warning("Tests did not clean up after themselves in %s" % self.root)

  def test_resolve_path(self):
    assert self.root + "/" == self.fs._resolve_path("/")
    assert self.root + "/foo" == self.fs._resolve_path("/foo")
    with pytest.raises(fs.IllegalPathException):
      self.fs._resolve_path("/../foo")
    # These are preserved, but that should be ok.
    assert self.root + "/bar/../foo" == self.fs._resolve_path("/bar/../foo")

  def test_open_and_remove(self):
    with pytest.raises(IOError):
      self.fs.open("/notfound", "r")
    f = self.fs.open("/x", "w")
    f.write("Hello world\n")
    f.close()

    f = self.fs.open("/x")
    assert "Hello world\n" == f.read()
    f.close()

    self.fs.remove("/x")

  def test_rename(self):
    # No exceptions means this worked fine.
    self.fs.open("/x", "w").close()
    self.fs.rename("/x", "/y")
    self.fs.remove("/y")

  def test_listdir(self):
    self.fs.mkdir("/abc")
    self.fs.open("/abc/x", "w").close()
    self.fs.open("/abc/y", "w").close()

    assert ["abc"] == self.fs.listdir("/")
    assert ["x", "y"] == sorted(self.fs.listdir("/abc"))

    self.fs.remove("/abc/x")
    self.fs.remove("/abc/y")
    self.fs.rmdir("/abc")

  def test_listdir_stats(self):
    self.fs.mkdir("/abc")
    self.fs.open("/abc/x", "w").close()
    self.fs.open("/abc/y", "w").close()

    stats = self.fs.listdir_stats("/")
    assert ["/abc"] == [s['path'] for s in stats]
    assert ["/abc/x", "/abc/y"] == sorted(s['path'] for s in self.fs.listdir_stats("/abc"))

    self.fs.remove("/abc/x")
    self.fs.remove("/abc/y")
    self.fs.rmdir("/abc")


  def test_keyword_args(self):
    # This shouldn't work!
    with pytest.raises(TypeError):
      self.fs.open(name="/foo", mode="w")


@pytest.mark.integration
@pytest.mark.requires_hadoop
def test_hdfs_copy():
  minicluster = pseudo_hdfs4.shared_cluster()
  minifs = minicluster.fs

  copy_test_src = minicluster.fs_prefix + '/copy_test_src'
  copy_test_dst = minicluster.fs_prefix + '/copy_test_dst'
  try:
    data = "I will not make flatulent noises in class\n" * 2000
    minifs.create(copy_test_src, permission=0o646, data=data)
    minifs.create(copy_test_dst, data="some initial data")

    minifs.copyfile(copy_test_src, copy_test_dst)
    actual = minifs.read(copy_test_dst, 0, len(data) + 100)
    assert data == actual

    sb = minifs.stats(copy_test_dst)
    assert 0o646 == stat.S_IMODE(sb.mode)
  finally:
    minifs.do_as_superuser(minifs.rmtree, copy_test_src)
    minifs.do_as_superuser(minifs.rmtree, copy_test_dst)


@pytest.mark.integration
@pytest.mark.requires_hadoop
def test_hdfs_full_copy():
  minicluster = pseudo_hdfs4.shared_cluster()
  minifs = minicluster.fs
  minifs.setuser('test')

  prefix = minicluster.fs_prefix + '/copy_test'
  try:
    minifs.mkdir(prefix)
    minifs.mkdir(prefix + '/src')
    minifs.mkdir(prefix + '/dest')

    # File to directory copy.
    # No guarantees on file permissions at the moment.
    data = "I will not make flatulent noises in class\n" * 2000
    minifs.create(prefix + '/src/file.txt', permission=0o646, data=data)
    minifs.copy(prefix + '/src/file.txt', prefix + '/dest')
    assert minifs.exists(prefix + '/dest/file.txt')

    # Directory to directory copy.
    # No guarantees on directory permissions at the moment.
    minifs.copy(prefix + '/src', prefix + '/dest', True)
    assert minifs.exists(prefix + '/dest/src')

    # Copy directory to file should fail.
    try:
      minifs.copy(prefix + '/src', prefix + '/dest/file.txt', True)
    except IOError:
      pass
    except Exception:
      raise
  finally:
    minifs.do_as_superuser(minifs.rmtree, prefix)


@pytest.mark.integration
@pytest.mark.requires_hadoop
def test_hdfs_copy_from_local():
  minicluster = pseudo_hdfs4.shared_cluster()
  minifs = minicluster.fs
  minifs.setuser('test')

  path = os.path.join(tempfile.gettempdir(), 'copy_test_src')
  logging.info(path)

  data = "I will not make flatulent noises in class\n" * 2000
  f = open(path, 'w')
  f.write(data)
  f.close()

  copy_dest = minicluster.fs_prefix + '/copy_test_dst'

  minifs.copyFromLocal(path, copy_dest)
  actual = minifs.read(copy_dest, 0, len(data) + 100)
  assert data == actual


if __name__ == "__main__":
  logging.basicConfig()
  unittest.main()
