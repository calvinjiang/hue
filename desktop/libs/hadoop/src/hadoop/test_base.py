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


import pytest
from builtins import object
from hadoop import pseudo_hdfs4
from django.test import TestCase

@pytest.mark.requires_hadoop
@pytest.mark.integration
class PseudoHdfsTestBase(TestCase):

  @classmethod
  def setup_class(cls):
    cls.cluster = pseudo_hdfs4.shared_cluster()
    cls.fs = cls.cluster.fs
