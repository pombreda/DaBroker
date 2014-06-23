# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division, unicode_literals
##
## This is part of DaBroker, a distributed data access manager.
##
## DaBroker is Copyright © 2014 by Matthias Urlichs <matthias@urlichs.de>,
## it is licensed under the GPLv3. See the file `README.rst` for details,
## including an optimistic statements by the author.
##
## This paragraph is auto-generated and may self-destruct at any time,
## courtesy of "make update". The original is in ‘utils/_boilerplate.py’.
## Thus, please do not remove the next line, or insert any blank lines.
##BP

# generic test setup

import logging,sys,os
logger = logging.getLogger("tests")

def test_init(who):
    if os.environ.get("TRACE","0") == '1':
        level = logging.DEBUG
    else:
        level = logging.WARN

    logger = logging.getLogger(who)
    logging.basicConfig(stream=sys.stderr,level=level)

    return logger

# local queue implementation

try:
    from queue import Queue
except ImportError:
    from Queue import Queue

class RPCmessage(object):
    msgid = None
    def __init__(self,p,msg):
        self.p = p
        self.msg = msg

    def reply(self,msg):
        logger.debug("Reply to %s with %r", self.msgid,msg)
        msg = RPCmessage(self.p,msg)
        msg.msgid = self.msgid
        self.p.reply_q.put(msg)
        
class ServerQueue(object):
    def __init__(self,p,worker):
        self.p = p
        self.worker = worker

    def _worker(self,msg):
        try:
            res = self.worker(msg.msg)
        except Exception as e:
            res = sys.exc_info()
        msg.reply(res)

    def _reader(self):
        from gevent import spawn
        while True:
            logger.debug("Server: wait for message")
            msg = self.p.request_q.get()
            logger.debug("Server: get msg %s",msg.msgid)
            spawn(self._worker,msg)
    
class ClientQueue(object):
    def __init__(self,p):
        self.p = p
        self.q = {}
        self.next_id = 1

    def _reader(self):
        while True:
            msg = self.p.reply_q.get()
            r = self.q.pop(msg.msgid,None)
            if r is not None:
                r.set(msg.msg)
        
    def send(self,msg):
        from gevent.event import AsyncResult

        msg = RPCmessage(self.p,msg)
        msg.msgid = self.next_id
        res = AsyncResult()
        self.q[self.next_id] = res
        self.next_id += 1

        logger.debug("Client: send %s with %r",msg.msgid,msg.msg)
        self.p.request_q.put(msg)
        logger.debug("Client: wait for %s",msg.msgid)
        res = res.get()
        logger.debug("Client: get %s with %r",msg.msgid,msg.msg)
        return res
    
class LocalQueue(object):
    def __init__(self,worker):
        from gevent import spawn

        self.worker = worker
        self.request_q = Queue()
        self.reply_q = Queue()

        sq = ServerQueue(self,self.worker)
        cq = ClientQueue(self)
        self.server = spawn(sq._reader)
        self.client = spawn(cq._reader)
        self.cq = cq

    def send(self,msg):
        return self.cq.send(msg)

    def shutdown(self):
        r = self.client; self.client = None
        if r is not None:
            r.kill()

        r = self.server; self.server = None
        if r is not None:
            r.kill()

