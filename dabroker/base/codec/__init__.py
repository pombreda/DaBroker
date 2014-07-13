#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import print_function,absolute_import
import sys
from time import mktime
from ...util import TZ,UTC, format_dt
from ...util.thread import local_stack
from ..config import default_config
import datetime as dt
from collections import namedtuple

from traceback import format_tb
import logging
logger = logging.getLogger("dabroker.base.codec")

current_loader = local_stack()

class _notGiven: pass
class ComplexObjectError(Exception): pass

DecodeRef = namedtuple('DecodeRef',('oid','parent','offset', 'cache'))
# This is used to store references to to-be-amended objects.
# The idea is that if a newly-decoded object encounters this, it can
# replace the offending reference by looking up the result in the cache.
# 
# Currently, this type only provides a hint about the problem's origin if
# it is ever encountered outside the decoding process. The problem does not
# occur in actual code because most objects are just transmitted as
# references, to be evaluated later (when an attribute referring to the
# object is accessed).

class SupD(dict):
	"""A dictionary which finds classes"""
	def get(self,k,default=_notGiven):
		"""Look up type K according to the name of its class, or its closest constituent"""
		if hasattr(k,"__mro__"):
			for x in k.__mro__:
				try:
					return self.__getitem__(x.__module__+"."+x.__name__)
				except KeyError:
					pass
		if default is _notGiven:
			raise KeyError(k)
		return default

class ServerError(Exception):
	"""An encapsulation for a server error (with traceback)"""
	def __init__(self,err,tb):
		self.err = err
		self.tb = tb

	def __repr__(self):
		return "ServerError({})".format(repr(self.err))

	def __str__(self):
		r = repr(self)
		if self.tb is None: return r
		return r+"\n"+"".join(self.tb)

_basics = []
def codec_adapter(cls):
	"""A decorator for an adapter class which translates serializer to whatever."""
	_basics.append(cls)
	return cls

@codec_adapter
class _datetime(object):
	cls = dt.datetime
	clsname = "datetime"

	@staticmethod
	def encode(obj, include=False):
		## the string is purely for human consumption and therefore does not have a time zone
		return {"t":mktime(obj.timetuple()),"s":format_dt(obj)}

	@staticmethod
	def decode(t=None,s=None,a=None,k=None,**_):
		if t:
			return dt.datetime.utcfromtimestamp(t).replace(tzinfo=UTC).astimezone(TZ)
		else: ## historic
			assert a
			return dt.datetime(*a).replace(tzinfo=TZ)

@codec_adapter
class _timedelta(object):
	cls = dt.timedelta
	clsname = "timedelta"

	@staticmethod
	def encode(obj, include=False):
		## the string is purely for human consumption and therefore does not have a time zone
		return {"t":obj.total_seconds(),"s":str(obj)}

	@staticmethod
	def decode(t,s=None,**_):
		return dt.timedelta(0,t)

@codec_adapter
class _date(object):
	cls = dt.date
	clsname = "date"

	@staticmethod
	def encode(obj, include=False):
		return {"d":obj.toordinal(), "s":obj.strftime("%Y-%m-%d")}

	@staticmethod
	def decode(d=None,s=None,a=None,**_):
		if d:
			return dt.date.fromordinal(d)
		## historic
		return dt.date(*a)

@codec_adapter
class _time(object):
	cls = dt.time
	clsname = "time"

	@staticmethod
	def encode(obj, include=False):
		ou = obj.replace(tzinfo=UTC)
		secs = ou.hour*3600+ou.minute*60+ou.second
		return {"t":secs,"s":"%02d:%02d:%02d" % (ou.hour,ou.minute,ou.second)}

	@staticmethod
	def decode(t=None,s=None,a=None,k=None,**_):
		if t:
			return dt.datetime.utcfromtimestamp(t).time()
		return dt.time(*a)

scalar_types = {type(None),float,bytes}
from six import string_types,integer_types
for s in string_types+integer_types: scalar_types.add(s)
scalar_types = tuple(scalar_types)

class BaseCodec(object):
	"""\
		Serialize my object structure to something dict/list-based and
		non-self-referential, suitable for JSON/BSON/XML/whatever-ization.

		@loader is something with a .get method. The resolving code will
		call that with a key if it needs to refer to an object.

		@adapters is a list of additional adapters which are to be
		registered.
		"""
	try_simple = 1000

	def __init__(self,loader,adapters=(), cfg={}):
		super(BaseCodec,self).__init__()
		self.loader = loader
		self.cfg = default_config.copy()
		self.cfg.update(cfg)
		self.type2cls = SupD() # encoder
		self.name2cls = {} # decoder 
		self.register(_basics)
		self.register(adapters)
	
	def register(self,cls):
		"""\
			Register more adapters.
			"""
		if isinstance(cls,(list,tuple)):
			for c in cls:
				self.register(c)
			return
		if cls.cls is not None:
			self.type2cls[cls.cls.__module__+"."+cls.cls.__name__] = cls
		if cls.clsname is not None:
			self.name2cls[cls.clsname] = cls
		
	def _encode(self, data, objcache,objref, include=False):
		# @objcache: dict: id(obj) => (seqnum,encoded)
		#            `encoded` will be set to the encoded object so that
		#            the seqnum can be removed later if it turns out not to
		#            be needed.
		# 
		# @objref: set: the seqnums which are actually required for proper
		#          encoding. If `None`, try to do simple encoding.
		
		# Scalars (integers, strings) do not refer to other objects and
		# thus are never encoded.
		#
		# The only case where that would help is long strings which are
		# referred to multiple times from the same object tree. This is too
		# unlikely to be worth the bother.
		if isinstance(data,scalar_types):
			return data

		# Have I seen that before?
		did = id(data)
		oid = objcache.get(did,None)
		if oid is not None:
			# Yes.
			if objref is None:
				raise ComplexObjectError(data)
			# Point to it.
			oid = oid[0]
			objref.add(oid)
			return {'_or':oid}
		# No. Generate a seqnum for it.
		oid = 1+len(objcache)
		objcache[did] = (oid,None)
		
		if isinstance(data,(list,tuple)):
			# A toplevel list will keep its "include" state
			data = type(data)(self._encode(x,objcache,objref,include) for x in data)
			if objref is None:
				return data

			res = { '_o':'LIST','_oi':oid,'_d':data }
			objcache[did] = (oid,res)
			return res

		odata = data
		if not isinstance(data,dict):
			obj = self.type2cls.get(type(data),None)
			if obj is None:
				raise NotImplementedError("I don't know how to encode %s: %r"%(repr(data.__class__),data,))
			data = obj.encode(data, include=include)

		res = type(data)()
		for k,v in data.items():
			if k.startswith('_o'):
				nk = '_o_'+k[2:]
			else:
				nk = k
			# if `include` is None, keep that value.
			res[nk] = self._encode(v,objcache,objref, include=False if include else include)
		if not isinstance(odata,dict):
			res['_o'] = obj.clsname
		if objref is not None:
			res['_oi'] = oid
		objcache[did] = (oid,res)
		return res

	def encode(self, data, include=False):
		"""\
			Encode this data structure.

			This code first tries to monitor whether the data structure in
			question is a proper tree (`try_simple` is 1000).

			If it encounters one that is not, it resets `try_simple` to
			zero and uses the full reference-tagging approach.
		
			@include: a flag telling the system to encode an object's data,
			          not just a reference. Used server>client. If None,
			          send object keys without retrieval info. This is used
			          e.g. when broadcasting, so as to not leak data access.
			"""
		if self.try_simple >= 1000:
			# Try to do a faster encoding pass
			try:
				objcache = {}
				res = self._encode(data, objcache,None, include=include)
			except ComplexObjectError:
				self.try_simple = 0

		if self.try_simple < 1000:
			# No, not yet / did not work: slower path
			objcache = {}
			objref = set()
			res = self._encode(data, objcache,objref, include=include)

			if objref:
				# At least one reference was required.
				self.try_simple = 0
				for i,v in objcache.values():
					if i not in objref:
						del v['_oi']
			else:
				# No, this was a proper tree after all.
				self.try_simple += 1
				for i,v in objcache.values():
					del v['_oi']
		return res
	
	def encode_error(self, err, tb=None):
		"""\
			Special method for encoding an error, with optional traceback.

			Note that this will not pass through the normal encoder, so the
			data should be strings (or, in case of the traceback, a list of
			strings).
			"""
		if not hasattr(err,'swapcase'): # it's a string
			err = str(err)
		res = {'_error':err }

		if tb is not None:
			if hasattr(tb,'tb_frame'):
				tb = format_tb(tb)
			res['tb'] = tb
		return res

	def _decode(self,data, objcache,objtodo, p=None,off=None):
		# Decode the data recursively.
		#
		# @objcache: dict seqnum=>result
		# 
		# @objtodo: Fixup data, list of (seqnum,parent,index). See below.
		#
		# @p, @off: parent object and index which refer to this object.
		#
		# During decoding, information to recover an object may not be
		# available, i.e. we encounter an object reference before or even
		# while decoding the data it refers to. The @objtodo array records
		# where the actual result is supposed to be stored, as soon as we
		# have it.
		#
		# TODO: This process does not yet work with object references
		# within other objects. To be implemented if needed.

		if isinstance(data, scalar_types):
			return data

		# "Unmolested" lists are passed through.
		if isinstance(data,(list,tuple)):
			return type(data)(self._decode(v,objcache,objtodo) for v in data)

		if isinstance(data,dict):
			oid = data.pop('_oi',None)
			obj = data.pop('_o',None)
			objref = data.pop('_or',None)
			if objref is not None:
				res = objcache.get(objref,None)
				if res is None:
					# Save fixing the problem for later
					res = DecodeRef(objref,p,off, objcache)
					objtodo.append(res)
				return res

			if obj == 'LIST':
				res = []
				if oid is not None:
					objcache[oid] = res
				k = 0
				for v in data['_d']:
					res.append(self._decode(v,objcache,objtodo, res,k))
					k += 1
				return res
			
			res = {}
			for k,v in data.items():
				if k.startswith("_o"):
					nk = '_o'+k[3:]
				else:
					nk = k
				res[nk] = self._decode(v,objcache,objtodo, res,k)

			if obj is not None:
				try:
					res = self.name2cls[obj].decode(**res)
				except Exception:
					logger.error("Decoding: %s: %r %r",obj,self.name2cls[obj],res)
					import pdb;pdb.set_trace()
					raise
			if oid is not None:
				objcache[oid] = res
			return res

		raise NotImplementedError("Don't know how to decode %r"%data)
	
	def _cleanup(self, objcache,objtodo):
		# resolve the "todo" stuff
		for d,p,k,_ in objtodo:
			p[k] = objcache[d]
		
	def decode(self, data):
		"""\
			Decode the data.
			
			Reverse everything the encoder does as cleanly as possible.
			"""
		if isinstance(data,dict) and '_error' in data:
			raise ServerError(data['_error'],data.get('tb',None))

		try:
			current_loader.push(self.loader)
			objcache = {}
			objtodo = []
			res = self._decode(data,objcache,objtodo)
			self._cleanup(objcache,objtodo)
			return res
		finally:
			current_loader.pop()

