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

# Some sqlalchemy helpers. They should be in dabroker.server.loader.sqlalchemy,
# but the separation makes sense (setup there / production code here).

from .thread import local_object
from sqlalchemy.inspection import inspect
from functools import wraps
from contextlib import contextmanager
from ..base import BrokeredInfo

import logging
logger = logging.getLogger("dabroker.util.sqlalchemy")

class local_session_object(local_object):
	"""sqlalchemy caches things. Thus simply releasing a session from local
		context doesn't dereference it, hence doesn't roll it back.
		So we do this here."""
	def __release_local__(self):
		sess = self.__storage__.pop(self.__ident_func__(), None)
		if sess is not None:
			for s in sess.values():
				s.rollback()

_session = local_session_object()
_sqlite_warned = False

def session_maker(maker,name=None):
	"""Create a thread-local session, if it doesn't exist already"""
	if name is None:
		name = "sql"
	s = getattr(_session,name,None)
	if s is None:
		#logger.debug("new session")
		s = maker()
		setattr(_session,name,s)
		if s.transaction is None:
			s.begin()
		s._dab_wrapped = 0
	return s

@contextmanager
def session_wrapper(obj, maker=None):
	"""Provide a transactional scope around a series of operations."""
	if maker is None:
		if isinstance(obj,BrokeredInfo):
			loader = obj.loader
		else:
			import pdb;pdb.set_trace()
			loader = obj._dab.loader

		s_name = loader.id
		maker = loader.session
	else:
		s_name = None
	s = session_maker(maker,s_name)
	if not s._dab_wrapped:
		if s.transaction is None:
			s.begin()
		s._dab_wrapped = 1
	else:
		s._dab_wrapped += 1
		if ".dialects.sqlite." in s.bind.dialect.__class__.__module__:
			global _sqlite_warned
			if not _sqlite_warned:
				_sqlite_warned = True
				logger.warn("sqlite does not understand nested transactions")
			try:
				yield s
			finally:
				s._dab_wrapped -= 1
			return
		else:
			#logger.debug("existing session")
			s.begin_nested()
	try:
		yield s
	except:
		s.rollback()
		raise
	else:
		s.commit()
	finally:
		s._dab_wrapped -= 1
	# The session is _not_ destroyed at this point, object attribute access
	# needs to be available until the thread dies.

def with_session(fn):
	if isinstance(fn,type(session_wrapper)):
		@wraps(fn)
		def wrapper(self,*a,**k):
			with session_wrapper(self) as s:
				return fn(self,s, *a,**k)
		return wrapper
	else:
		maker = fn.session
		def wrapper2(fn):
			@wraps(fn)
			def wrapper(self,*a,**k):
				with session_wrapper(self,maker) as s:
					return fn(self,s, *a,**k)
			return wrapper
		return wrapper2

