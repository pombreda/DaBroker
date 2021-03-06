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

# This module implements the basic client server.

RETR_TIMEOUT = 10
CACHE_SIZE=10000

from ..base import UnknownCommandError,BaseRef
from ..base.transport import BaseCallbacks
from ..base.config import default_config
from ..base.codec import ServerError
from ..base.service import BrokerEnv
from ..util import import_string
from ..util.thread import spawned, AsyncResult
from .codec import adapters, client_broker_info_meta, search_key

import logging
logger = logging.getLogger("dabroker.client.service")

from weakref import WeakValueDictionary,KeyedRef,ref
from collections import deque
from heapq import heapify,heappop

class _NotGiven: pass

class KnownSearch(object):
	def __init__(self, kw, res, ckey, limit=0):
		self.kw = kw
		self.res = res
		self.ckey = ckey
		self.limit = limit

class ExtKeyedRef(KeyedRef):
	"""A KeyedRef which includes an access counter."""

	__slots__ = "key","counter"

	def __new__(type, ob, callback, key):
		self = ref.__new__(type, ob, callback)
		self.key = key
		self.counter = 0
		return self

	def __init__(self, ob, callback, key):
		super(ExtKeyedRef,  self).__init__(ob, callback, key)
	
	def __lt__(self,other):
		return self.counter > other.counter
		# Yes, this is backwards. That is intentional. See below.

class CountedCache(WeakValueDictionary,object):
	"""A WeakValueDictionary which counts accesses."""
	def __init__(self, *args, **kw):
		super(CountedCache,self).__init__(*args,**kw)
		def remove(wr, selfref=ref(self)):
			self = selfref()
			if self is not None:
				if self._iterating and hasattr(self,'_pending_removals'):
					self._pending_removals.append(wr.key)
				else:
					# Bug in Python's stdlib: the original does "del" here
					# which triggers an ignored error in test06
					self.data.pop(wr.key,None)
		self._remove = remove

	def __getitem__(self, key):
		r = self.data[key]
		o = r()
		if o is None:
			raise KeyError(key)
		else:
			r.counter += 1
			return o

	def get_ref(self, key, default=None):
		return self.data.get(key, default)

	def get_counter(self, key):
		r = self.data.get(key,None)
		if r is None:
			return -1
		return r.count

	def __setitem__(self, key, value):
		if getattr(self,'_pending_removals',False):
			self._commit_removals()

		ref = self.data.get(key, None)
		if ref is not None:
			ref.counter = -1
		self.data[key] = ExtKeyedRef(value, self._remove, key)

	def __delitem__(self, key):
		if getattr(self,'_pending_removals',False):
			self._commit_removals()

		ref = self.data.pop(key, None)
		if ref is not None:
			ref.counter = -1

	def setdefault(self, key, default=None):
		try:
			wr = self.data[key]
		except KeyError:
			if getattr(self,'_pending_removals',False):
				self._commit_removals()
			self.data[key] = ExtKeyedRef(default, self._remove, key)
			return default
		else:
			return wr()

	def update(self, dict=None, **kwargs):
		if getattr(self,'_pending_removals',False):
			self._commit_removals()
		d = self.data
		if dict is not None:
			if not hasattr(dict, "items"):
				dict = type({})(dict)
			for key, o in dict.items():
				ref = self.data.get(key, None)
				if ref is not None:
					ref.counter = -1
				d[key] = ExtKeyedRef(o, self._remove, key)
		if len(kwargs):
			self.update(kwargs)

class CacheDict(CountedCache):
	"""\
		This is an augmented WeakValueDict which keeps the last CACHE_SIZE items pinned.

		.lru is a hash which is used like a FIFO (I'd use collections.deque,
		except that a deque's length is not mutable).

		Items popping off the FIFO are added to a heap (sized CACHE_SIZE/10
		by default). The most-used half of these items are re-added to the FIFO, the rest is dropped.

		"""
	def __init__(self,*a,**k):
		self.lru = {}
		self.lru_next = 0
		self.lru_last = 0
		self.lru_size = CACHE_SIZE

		self.heap_min = CACHE_SIZE//20
		self.heap_max = CACHE_SIZE//10
		self.heap = []
		super(CacheDict,self).__init__(*a,**k)

	def set(self, key,value):
		"""\
			Set an item, but bypass the LRU code.
		
			Used for adding an interim value (AsyncResult while fetching the real thing).
			"""
		super(CacheDict,self).__setitem__(key,value)
		return value

	def __setitem__(self, key, value):
		super(CacheDict,self).__setitem__(key,value)
		id = self.lru_next; self.lru_next += 1
		self.lru[id] = (key,value)

		# Move items from the queue to the heap
		min_id = id - self.lru_size
		while self.lru_last < min_id:
			id = self.lru_last; self.lru_last += 1
			if id not in self.lru: continue
			key,value = self.lru[id]
			ref = self.data.get(key,None)
			if ref is not None:
				self.heap.append((ref,key,value))
			del self.lru[id]

		# When enough items accumulate on the heap:
		# Move the most-used items back to the queue
		# This block should not schedule.
		if len(self.heap) > self.heap_max:
			self.heap = [(r,k,v) for r,k,v in self.heap if r.counter >= 0]
			heapify(self.heap)
			while len(self.heap) > self.heap_min:
				ref,key,value = heappop(self.heap)
				if ref.counter > 1:
					id = self.lru_next; self.lru_next += 1
					ref.counter = 0
					self.lru[id] = (key,value)

			self.heap = [] ## optional

	def invalidate(self,key):
		obj = self.get(key,None)
		if isinstance(obj,(AsyncResult,type(None))):
			return
		obj = self.pop(key,None)
		if obj is None:
			return
		obj._obsolete = True
		obj._obsoleted()
		

class ChangeData(object):
	"""Some data has been changed locally. Remember which."""
	def __init__(self,server,obj):
		self.obj = obj
		self.old_data = {}

		server.obj_chg[obj._key] = self

	def send_commit(self,server):
		upd = {}
		obj = self.obj
		meta = obj._meta
		for k,ov in self.old_data.items():
			if k in meta.fields:
				nv = getattr(obj,k)
			elif k in meta.refs:
				ov = BaseRef(ov)
				nv = BaseRef(obj._refs[k])
			else:
				raise RuntimeError(k)
			if ov != nv:
				upd[k] = (ov,nv)
		if not upd:
			return None
		return server.send("update",self.obj._key,k=upd)

	def send_revert(self,server):
		for k,v in self.old_data.items():
			if k in self.obj._meta.fields:
				setattr(self.obj,k,v)
			else:
				self.obj._refs[k] = v

class ChangeNew(ChangeData):
	def send_revert(self,server):
		return server._send("delete",self.obj._key)

class ChangeDel(ChangeData):
	@property
	def obj(self):
		raise KeyError(self.obj._key)
	def send_commit(self,server):
		server._cache.invalidate(self.obj._key)
		return server._send("delete",self.obj._key)
	def send_revert(self,server):
		if self.obj not in server._cache:
			server._add_to_cache(self.obj)
		super(ChangeDel,self).revert(server)

class ChangeInvalid(ChangeData):
	def __init__(self,server,obj,coll):
		super(ChangeInvalid,self).__init__(server,obj)
		self.colliding = coll
	def send_commit(self,server):
		raise RuntimeError("inconsistent data",self.obj,self.coll)

class BrokerClient(BrokerEnv, BaseCallbacks):
	"""\
		The basic client implementation.
		"""
	root_key = None
	last_msgid = 0
	last_msgid_wait = None

	def __init__(self, cfg={}):
		global client
		assert client is None

		self.cfg = default_config.copy()
		self.cfg.update(cfg)
		self.trace = cfg.get('trace',0)

		self._cache = CacheDict()
		self.codec = self.make_codec()
		self.transport = self.make_transport()

		self._add_to_cache(client_broker_info_meta)
		self.obj_chg = {}

		self.register_codec(adapters)

	def start(self):
		self.transport.connect()
	
	def stop(self):
		self.transport.disconnect()

	def make_transport(self):
		name = self.cfg['transport']
		if '.' not in name:
			name = "dabroker.client.transport."+name+".Transport"
		return import_string(name)(cfg=self.cfg, callbacks=self)

	def make_codec(self, adapters=()):
		name = self.cfg['codec']
		if '.' not in name:
			name = "dabroker.base.codec."+name+".Codec"
		res = import_string(name)(loader=self, cfg=self.cfg)
		res.register(adapters)
		return res

	def register_codec(self,adapter):
		if self.codec is None:
			self._adapters.append(adapter)
		else:
			self.codec.register(adapter)

	def _add_to_cache(self, obj):
		key = getattr(obj,'_key',None)
		if key is None:
			old = None
		else:
			old = self._cache.get(key, None)
			if old is obj:
				return

		if old is None:
			self._cache[key] = obj
		elif isinstance(old,AsyncResult):
			self._cache[key] = obj
			old.set(obj)
		else:
			# We get an object we already have. Locally modified?
			chg = self.obj_chg.get(key,None)
			if chg is not None:
				# Ugh. Yes.
				upd = {}
				coll = {}
				for k in obj._meta.fields:
					sv = getattr(obj,k) # new from server
					cv = old.__dict__.get(k,None) # new on the client
					ov = chg.old_data.get(k,cv) # our old value
					if cv == sv:
						# server has our current value, so drop that change
						chg.old_data.pop(k,None)
						continue
					if ov == sv:
						# server didn' yet see our change, nothing to do
						continue
					if cv != ov:
						# three-way difference: inconsistent values
						coll[k] = (ov,cv,sv)
						continue
					upd[k] =  sv
				if coll:
					self.obj_chg[key] = ChangeInvalid(self,obj,coll)
					return old
				# 
				old.__dict__.update(upd)
				if not chg.old_data:
					# all our updates have arrived on the server
					del self.obj_chg[key]
			else:
				old.__dict__.update(obj.__dict__)
			return old
		obj._dab = self
		return obj

	def get(self, key):
		"""Get an object, from cache or from the server."""

		# Step 1: if we locally changed the object, return our copy.
		chg = self.obj_chg.get(key,None)
		if chg is not None:
			return chg.obj

		# Step 2: Get it from cache.
		# If the cache contains an AsyncResult, wait for that.
		obj = self._cache.get(key,None)
		if obj is not None:
			if isinstance(obj,AsyncResult):
				obj = obj.get(timeout=RETR_TIMEOUT)
			return obj

		# Step 3: Get it from the network.
		# Add an AsyncResult to the cache so that the object is not
		# retrieved multiple times in parallel.
		ar = self._cache.set(key, AsyncResult())
		try:
			obj = self.send("get",key)
		except Exception as e:
			# Owch.
			# Remove the AsyncResult from cache, and forward the exception to any waiters
			arx = self._cache.pop(key)
			logger.exception("Ouch %r %r",ar,arx)
			assert ar is arx, (ar,arx)
			ar.set_exception(e)
			# As `ar` is unused beyond this point, its value might already
			# gone from the cache (weak reference!) if we just use the
			# return value of ._cache_pop()
			raise
		else:
			# The deserializer has already added the object to the cache (or it should have)
			cobj = self._cache.get(key,None)
			assert cobj is obj, (cobj,obj,key)
			return obj
		
	def obj_new(self,cls,**kw):
		obj = self.send("new",cls,kw)
		ChangeNew(self,obj)
		return obj

	def obj_del(self,obj):
		ChangeDel(self,obj)

	def obj_change(self,obj,k,ov,nv):
		if ov == nv: return
		chg = self.obj_chg.get(obj._key,None)
		if chg is None:
			chg = ChangeData(self,obj)
		chg.old_data.setdefault(k,ov)
	
	def commit(self):
		chg = self.obj_chg; self.obj_chg = {}
		try:
			res = []
			for v in chg.values():
				r = v.send_commit(self)
				if isinstance(r,AsyncResult):
					res.append(r)
			for r in res:
				r.get(timeout=RETR_TIMEOUT)
		except:
			self._rollback(chg)
			raise
	def _rollback(self,chg):
		res = []
		for v in chg.values():
			r = v.send_revert(self)
			if isinstance(r,AsyncResult):
				res.append(r)
		for r in res:
			r.get(RETR_TIMEOUT)
		
	def rollback(self):
		chg = self.obj_chg; self.obj_chg = {}
		self._rollback(chg)
	
	def find(self, typ, _cached=False,_limit=None, **kw):
		"""Find objects by keyword"""
		assert getattr(typ.calls.get('_dab_search',None),'for_class',False)
		
		if _cached:
			kws = search_key(None,**kw)
			ks = typ.searches.get(kws,None)
			if ks is not None and (not ks.limit or (_limit and _limit <= len(ks.res))):
				self._cache[ks.ckey] # update the access counter
				if _limit:
					return ks.res[:_limit]
				else:
					return ks.res
		
		kw['_obj'] = typ
		if _limit is not None:
			kw['_limit'] = _limit
		res = self.send("_dab_search", **kw)

		if _cached:
			ckey = " ".join(str(x) for x in typ._key.key)+":"+kws

			if _limit and len(res) < _limit:
				_limit = None
			ks = KnownSearch(kw,res,ckey, _limit)
			typ.searches[kws] = ks
			self._cache[ckey] = ks

		return res

	def count(self, typ, _cached=False, **kw):
		"""Count objects"""
		call = typ._meta.calls['_dab_count']
		if _cached:
			kws = search_key(None,_c='count',**kw)
			ks = typ.searches.get(kws,None)
			if ks is not None:
				self._cache[ks.ckey] # update the access counter
				return ks.res
		
		kw['_obj'] = typ
		res = self.send("_dab_count", **kw)

		if _cached:
			ckey = " ".join(str(x) for x in typ._key.key)+":"+kws
			ks = KnownSearch(kw,res,ckey)
			typ.searches[kws] = ks
			self._cache[ckey] = ks
		return res

	def call(self, obj,name,a,k, _meta=False):
		if _meta:
			k['_mt']=True
		res = self.send(name,_obj=obj,*a,**k)
		return res
		
	def do_ping(self):
		"""The server wants to know who's listening. So tell it."""
		if self.trace:
			logger.debug("ping %r",msg)
		self.send("pong")

	def do_pong(self):
		# for completeness. The server doesn't send a broadcast on client request.
		raise RuntimeError("This can't happen")

	def do_signal(self, _obj,_sig, **data):
		if not isinstance(_obj,BaseRef):
			_obj = _obj._key
		_obj.send(_sig,**data)

	def do_invalid(self,*keys):
		"""Directly invalidate these cache entries."""
		for k in keys:
			try:
				self._cache.invalidate(k)
			except KeyError:
				pass

	def do_invalid_key(self,_key=None,_meta=None, **k):
		"""Invalidate an object, plus whatever might have been used to search for it.
		
			@key the updated/deleted object (or None if the object is new)
			@meta the object's metadata key (search results hang off metadata)
			@k: a key=>(value,…) dict. A search is obsoleted when one
									   of the search keys matches one of the values.
			"""
		if _key is not None:
			#logger.debug("inval_key: %r: %r",_key,k)
			self._cache.invalidate(_key)

		if _meta is None:
			#logger.warn("no metadata?")
			return
		obj = self._cache.get(_meta,None)
		if obj is None:
			#logger.warn("metadata not found: %s for %s",_meta,_key)
			return
		if isinstance(obj,AsyncResult):
			#logger.debug("inval_key: wait for %r",_meta)
			obj = obj.get(timeout=RETR_TIMEOUT)
			#logger.debug("inval_key: wait for %r: got %r",_meta,obj)
		#logger.warn("inval start %s %s",obj,k)

		obsolete = set()

		# What this does: a search checks a number of keys for specific
		# values. So the search is affected when all of these values 
		# match our update set.
		# A search is also affected when none of the values match, but only
		# if it's not an update.
		# TODO: This loop is somewhat inefficient.
		for ks,s in obj.searches.items():
			#logger.warn("Scanning %s %s",ks,s)
			keymatches = False
			mismatches = False
			is_update = False
			for i,v in k.items():
				if len(v) > 1:
					is_update = True
				sv = s.kw.get(i,_NotGiven)
				if sv is _NotGiven:
					continue
				keymatches = True
				if sv not in v:
					mismatches = True
					break
			if not mismatches if keymatches else not is_update:
				obsolete.add(ks)
		for ks in obsolete:
			#logger.debug("dropping %s",ks)
			obj.searches.pop(ks,None)
		#logger.warn("inval done %s",obj)

	@property
	def root(self):
		"""Get the object root. This may or may not be a cacheable object."""
		rk = self.root_key
		if rk is not None:
			if isinstance(rk,AsyncResult):
				return rk.get(timeout=RETR_TIMEOUT)
			return self.get(self.root_key)

		self.root_key = rk = AsyncResult()
		try:
			obj = self.send("root")
		except Exception as e:
			self.root_key = None
			rk.set_exception(e)
			raise
		self.root_key = getattr(obj,"_key",None)
		if self.root_key is not None:
			self._add_to_cache(obj)
		rk.set(obj)
		return obj

	def send(self, action, *a,**kw):
		"""Generic method for RPCing the server"""
		_obj = kw.pop('_obj',None)
		#logger.debug("send %s %r %r %r",action,_obj,a,kw)
		assert '_a' not in kw
		assert '_m' not in kw
		assert '_o' not in kw

		kw['_m'] = action
		if _obj is not None:
			kw['_o'] = _obj
		if a:
			kw['_a'] = a
		msg = self._send(kw)
		#logger.debug("recv %r",msg)
		return msg
	
	def _send(self,msg):
		"""Low-level message sender"""
		with self.env:
			#logger.debug("Send req: %r",msg)
			msg = self.codec.encode(msg)
			msg = self.transport.send(msg)
			msg = self.codec.decode(msg)
			if hasattr(msg,'msgid'):
				msgid = msg.msgid
				while self.last_msgid < msgid:
					if self.trace:
						logger.debug("Waiting %d %d",self.last_msgid,msgid)
					if self.last_msgid_wait is None:
						self.last_msgid_wait = AsyncResult()
					chk = self.last_msgid_wait.get()
					if chk >= msgid:
						break

			#logger.debug("Recv reply: %r",msg)
			msg = self.codec.decode2(msg)
			return msg

	@spawned
	def recv(self, msg):
		"""Process incoming notifications from the server"""
		#logger.debug("bcast raw %r",msg)
		with self.env:
			try:
				msg = self.codec.decode(msg)
			except ServerError as e:
				logger.exception("Server sends us an error. Shutting down.")
				self.end()
				return
			msgid = msg.get('msgid',None)
			msg = self.codec.decode2(msg)
			#logger.debug("bcast %r",msg)
			m = msg.pop('_m')
			a = msg.pop('_a',())

			try:
				proc = getattr(self,'do_'+m)
			except AttributeError:
				raise UnknownCommandError(m)
			proc(*a,**msg)

			if self.trace:
				logger.debug("LastID %s %s",self.last_msgid,msgid)
			if msgid and self.last_msgid < msgid:
				self.last_msgid = msgid
				x,self.last_msgid_wait = self.last_msgid_wait,None
				if x is not None:
					x.set(msgid)


client = None
