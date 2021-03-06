# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division, unicode_literals
##
## This file is part of DaBroker, a distributed data access manager.
##
## DaBroker is Copyright © 2014 by Matthias Urlichs <matthias@urlichs.de>,
## it is licensed under the GPLv3. See the file `README.rst` for details,
## including optimistic statements by the author.
##
## This paragraph is auto-generated and may self-destruct at any time,
## courtesy of "make update". The original is in ‘utils/_boilerplate.py’.
## Thus, please do not remove the next line, or insert any blank lines.
##BP

# This test mangles SQL, courtesy of sqlalchemy.

import os
import sys
from dabroker.server.service import BrokerServer
from dabroker.server.loader.sqlalchemy import SQLLoader
from dabroker.base import BrokeredInfo, Field, BaseObj
from dabroker.util import cached_property
from dabroker.util.thread import AsyncResult

from dabroker.util.tests import test_init
logger = test_init("test.30.amqp.server")

from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy import create_engine
 
Base = declarative_base()
 
# Standard SQLAlchemy example

class Person(Base):
	__tablename__ = 'person'
	_dab_cached=True
	# Here we define columns for the table person
	# Notice that each column is also a normal Python instance attribute.
	id = Column(Integer, primary_key=True)
	name = Column(String(250), nullable=False)
 
class Address(Base):
	__tablename__ = 'address'
	_dab_cached=True
	# Here we define columns for the table address.
	# Notice that each column is also a normal Python instance attribute.
	id = Column(Integer, primary_key=True)
	street_name = Column(String(250))
	street_number = Column(String(250))
	post_code = Column(String(250), nullable=False)
	person_id = Column(Integer, ForeignKey('person.id'))
	person = relationship(Person,backref='addrs')

try:
	os.unlink('/tmp/test30.db')
except EnvironmentError:
	pass
engine = create_engine('sqlite:////tmp/test30.db', echo=(True if os.environ.get('TRACE',False) else False))
Base.metadata.create_all(engine)

DBSession = sessionmaker(bind=engine)

class TestServer(BrokerServer):
	@cached_property
	def root(self):
		rootMeta = BrokeredInfo("rootMeta")
		rootMeta.add(Field("hello"))
		rootMeta.add(Field("data"))
		self.add_static(rootMeta,1)

		class RootObj(BaseObj):
			_meta = rootMeta
			hello = "Hello!"
			data = {}

		root = RootObj()
		self.add_static(root,2,30)
		return root

	def start(self):
		super(TestServer,self).start(purge=True)
		
	def __init__(self,*a,**k):
		super(TestServer,self).__init__(*a,**k)

		sql = SQLLoader(DBSession,self)
		sql.add_model(Person,self.root.data, rw=True)
		sql.add_model(Address)

	def do_trigger(self,msg):
		self.send("trigger",msg)
	
	def do_check(self,msg):
		# C: check
		session = DBSession()
		res = list(session.query(Person))
		assert len(res)==1
		res = res[0]
		assert res.name==msg,res.name

	def do_list_me(self,msg):
		# C: check
		session = DBSession()
		return list(session.query(Person))


