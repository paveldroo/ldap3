"""
Created on 2014.03.23

@author: Giovanni Cannata

Copyright 2014 Giovanni Cannata

This file is part of python3-ldap.

python3-ldap is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

python3-ldap is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with python3-ldap in the COPYING and COPYING.LESSER files.
If not, see <http://www.gnu.org/licenses/>.
"""
from datetime import datetime
from os import linesep
try:
    from queue import Empty
except ImportError:  # Python 2
    # noinspection PyUnresolvedReferences
    from Queue import Empty
from time import sleep
from multiprocessing import Process, Lock, JoinableQueue, Queue, Pool, cpu_count
from .. import REUSABLE_THREADED_POOL_SIZE, REUSABLE_THREADED_LIFETIME, STRATEGY_SYNC_RESTARTABLE, TERMINATE_REUSABLE, RESPONSE_WAITING_TIMEOUT, LDAP_MAX_INT, RESPONSE_SLEEPTIME
from .baseStrategy import BaseStrategy
from ..core.usage import ConnectionUsage
from ..core.exceptions import LDAPConnectionPoolNameIsMandatoryError, LDAPConnectionPoolNotStartedError, LDAPOperationResult, LDAPExceptionError


# noinspection PyProtectedMember
from ldap3 import REUSABLE_PARALLEL_NUMBER_OF_THREADS, STRATEGY_REUSABLE_THREADED


class ReusableParallelStrategy(BaseStrategy):
    """
    A pool of reusable ReusableThreadedStrategy connections with lazy behaviour and limited lifetime.
    The connection using this strategy presents itself as a normal connection, but internally the strategy has a pool of
    connections that can be used as needed. Each connection has multiple threads and lives in its own process and has a busy/available status.
    The strategy performs the requested operation on the first available connection.
    The pool of connections is instantiated at strategy initialization.
    Strategy has 3 customizable properties, the total number of connections in the pool, the number of threads in each connection and the lifetime of each connection.
    When lifetime is expired no more operations are sent to the connection that when idle will be closed and open again when needed.
    """
    pools = dict()

    # noinspection PyProtectedMember
    class ConnectionPool(object):
        def __new__(cls, connection):
            if connection.pool_name in ReusableParallelStrategy.pools:  # returns existing connection pool
                pool = ReusableParallelStrategy.pools[connection.pool_name]
                if not pool.started:  # if pool is not started remove it from the pools singleton and create a new onw
                    del ReusableParallelStrategy.pools[connection.pool_name]
                    return object.__new__(cls)
                if connection.pool_lifetime and pool.lifetime != connection.pool_lifetime:  # change lifetime
                    pool.lifetime = connection.pool_lifetime
                if connection.pool_size and pool.pool_size != connection.pool_size:  # if pool size has changed terminate and recreate the connections
                    pool.terminate_pool()
                    pool.pool_size = connection.pool_size
                return pool
            else:
                return object.__new__(cls)

        def __init__(self, connection):
            if not hasattr(self, 'connections'):
                self.name = connection.pool_name
                self.original_connection = connection
                self.pool_size = connection.pool_size or (cpu_count() // 2 if cpu_count() and cpu_count() > 1 else 2)  # at least 2 parallel process, defaults to half of the cpu available
                self.connections = []
                self.lifetime = connection.pool_lifetime or REUSABLE_THREADED_LIFETIME
                self.threads = REUSABLE_PARALLEL_NUMBER_OF_THREADS
                self.request_queue = JoinableQueue()
                self.response_queue = Queue()
                self.open_pool = False
                self.bind_pool = False
                self.tls_pool = False
                self._incoming = dict()
                self.counter = 0
                self.terminated_usage = ConnectionUsage() if connection._usage else None
                self.terminated = False
                self.lock = Lock()
                ReusableParallelStrategy.pools[self.name] = self
                self.started = False


        def __str__(self):
            s = str(self.name) + ' - ' + ('started' if self.started else 'terminated') + linesep
            s += 'original connection: ' + str(self.original_connection) + linesep
            s += 'response pool length: ' + str(len(self._incoming))
            s += ' - pool size: ' + str(self.pool_size)
            s += ' - lifetime: ' + str(self.lifetime)
            s += ' - open: ' + str(self.open_pool)
            s += ' - bind: ' + str(self.bind_pool)
            s += ' - tls: ' + str(self.tls_pool)

            for connection in self.connections:
                s += linesep
                s += str(connection)

            return s

        def __repr__(self):
            return self.__str__()

        def start_pool(self):
            if not self.started:
                self.create_pool()
                for connection in self.connections:
                    connection.start()
                self.started = True
                return True
            return False

        def create_pool(self):
            self.connections = [ReusableParallelStrategy.ReusableParallelConnection(self.original_connection, self.request_queue) for _ in range(self.pool_size)]

        def terminate_pool(self):
            self.started = False
            self.request_queue.join()  # wait for all queue pending operations

            for _ in range(len([connection for connection in self.connections if connection.process.is_alive()])):  # put a TERMINATE signal on the queue for each active prcoess
                self.request_queue.put((TERMINATE_REUSABLE, None, None, None, None, None, None))

            self.request_queue.join()  # wait for all queue terminate operations

    class PooledConnectionProcess(Process):
        def __init__(self, reusable_connection, original_connection, request_queue, response_queue):
            Process.__init__(self)
            self.daemon = True
            self.active_connection = reusable_connection
            self.original_connection = original_connection
            self.request_queue = request_queue
            self.response_queue = response_queue

        # noinspection PyProtectedMember
        def run(self):
            self.active_connection.running = True
            terminate = False
            while not terminate:
                counter, message_type, request, controls, pool_open, pool_bind, pool_tls = self.request_queue.get()
                self.active_connection.busy = True
                if counter == TERMINATE_REUSABLE:
                    terminate = True
                    if self.active_connection.connection.bound:
                        try:
                            self.active_connection.connection.unbind()
                        except LDAPExceptionError:
                            pass
                else:
                    if (datetime.now() - self.active_connection.creation_time).seconds >= self.original_connection.strategy.pool.lifetime:  # destroy and create a new connection
                        try:
                            self.active_connection.connection.unbind()
                        except LDAPExceptionError:
                            pass
                        self.active_connection.new_connection()
                    if message_type not in ['bindRequest', 'unbindRequest']:
                        if pool_open and self.active_connection.connection.closed:
                            self.active_connection.connection.open()
                        if pool_bind and not self.active_connection.connection.bound:
                            self.active_connection.connection.bind()
                        if pool_tls and not self.active_connection.connection.tls_started:
                            self.active_connection.connection.start_tls()
                        # noinspection PyProtectedMember
                        self.active_connection.connection._fire_deferred()  # force deferred operations

                        exc = None
                        response = None
                        result = None
                        try:
                            if message_type == 'searchRequest':
                                response = self.active_connection.connection.post_send_search(self.active_connection.connection.send(message_type, request, controls))
                            else:
                                response = self.active_connection.connection.post_send_single_response(self.active_connection.connection.send(message_type, request, controls))
                            result = self.active_connection.connection.result
                        except LDAPOperationResult as e:  # raise_exceptions has raise an exception. It must be redirected to the original connection process
                            exc = e

                        self.response_queue.put((counter, exc, None) if exc else (counter, response, result))

                # self.original_connection.busy = False
                self.request_queue.task_done()
            #if self.original_connection.usage:
            #    pool.terminated_usage += self.active_connection.connection.usage
            self.active_connection.running = False

    class ReusableParallelConnection(object):
        """
        Container for the ReusableThreadedStrategy connection. it includes a process and a lock to execute the connection in the pool
        """
        def __init__(self, connection, request_queue, response_queue):

            self.original_connection = connection
            self.request_queue = request_queue
            self.response_queue = response_queue
            self.running = False
            self.busy = False
            self.connection = None
            self.creation_time = None
            self.new_connection()
            self.process = ReusableParallelStrategy.PooledConnectionProcess(self, connection, self.request_queue, self.response_queue)

        def __str__(self):
            s = str(self.connection) + linesep
            s += 'running ' if self.running else '-halted'
            s += ' - ' + ('busy' if self.busy else ' available')
            s += ' - ' + ('creation time: ' + self.creation_time.isoformat())

            return s

        def new_connection(self):
            from ..core.connection import Connection
            # noinspection PyProtectedMember
            self.connection = Connection(server=self.original_connection.server_pool if self.original_connection.server_pool else self.original_connection.server,
                                         user=self.original_connection.user,
                                         password=self.original_connection.password,
                                         version=self.original_connection.version,
                                         authentication=self.original_connection.authentication,
                                         client_strategy=STRATEGY_REUSABLE_THREADED,
                                         raise_exceptions=self.original_connection.raise_exceptions,
                                         check_names=self.original_connection.check_names,
                                         auto_referrals=self.original_connection.auto_referrals,
                                         sasl_mechanism=self.original_connection.sasl_mechanism,
                                         sasl_credentials=self.original_connection.sasl_credentials,
                                         collect_usage=True if self.original_connection._usage else False,
                                         read_only=self.original_connection.read_only,
                                         auto_bind=self.original_connection.auto_bind,
                                         lazy=True)

            if self.original_connection.server_pool:
                self.connection.server_pool = self.original_connection.server_pool
                for server in self.connection.server_pool:
                    server.lock = Lock()  # substitutes threading lock with multiprocessing lock
                self.connection.server_pool.initialize(self.connection)
            else:
                self.server.lock = Lock() # substitute threading lock with multiprocessing lock

            self.creation_time = datetime.now()

    def __init__(self, ldap_connection):
        BaseStrategy.__init__(self, ldap_connection)
        self.sync = False
        self.no_real_dsa = False
        self.pooled = True
        self.can_stream = False
        if hasattr(ldap_connection, 'pool_name') and ldap_connection.pool_name:
            self.pool = ReusableParallelStrategy.ConnectionPool(ldap_connection)
        else:
            raise LDAPConnectionPoolNameIsMandatoryError('reusable parallel connection must have a pool_name')

    def open(self, reset_usage=True):
        self.pool.open_pool = True
        self.pool.start_pool()
        self.connection.closed = False
        if self.connection._usage:
            if reset_usage or not self.connection._usage.initial_connection_start_time:
                self.connection._usage.start()

    def terminate(self):
        self.pool.terminate_pool()
        self.pool.open_pool = False
        self.connection.bound = False
        self.connection.closed = True
        self.pool.bind_pool = False
        self.pool.tls_pool = False

    def _close_socket(self):
        """
        Doesn't really close the socket
        """
        self.connection.closed = True

        if self.connection._usage:
            self.connection._usage.closed_sockets += 1

    def send(self, message_type, request, controls=None):
        if self.pool.started:
            if message_type == 'bindRequest':
                self.pool.bind_pool = True
                counter = -1  # -1 stands for bind request
            elif message_type == 'unbindRequest':
                self.pool.bind_pool = False
                counter = -2  # -1 stands for unbind request
            elif message_type == 'extendedReq' and self.connection.starting_tls:
                self.pool.tls_pool = True
                counter = -3  # -1 stands for start_tls extended request
            else:
                with self.pool.lock:
                    self.pool.counter += 1
                    if self.pool.counter > LDAP_MAX_INT:
                        self.pool.counter = 1
                    counter = self.pool.counter
                self.pool.request_queue.put((counter, message_type, request, controls, self.pool.open_pool, self.pool.bind_pool, self.pool.tls_pool))
            return counter
        raise LDAPConnectionPoolNotStartedError('reusable connection pool not started')

    def get_response(self, counter, timeout=RESPONSE_WAITING_TIMEOUT):
        if counter == -1:  # send a bogus bindResponse
            return list(), {'description': 'success', 'referrals': None, 'type': 'bindResponse', 'result': 0, 'dn': '', 'message': '', 'saslCreds': 'None'}
        elif counter == -2:  # bogus unbind
            return None
        elif counter == -3:  # bogus startTls extended request
            return list(), {'result': 0, 'referrals': None, 'responseName': '1.3.6.1.4.1.1466.20037', 'type': 'extendedResp', 'description': 'success', 'responseValue': 'None', 'dn': '', 'message': ''}
        response = None
        result = None
        while timeout >= 0:  # waiting for completed message to appear in _incoming
            try:  #checks if response already received
                with self.connection.strategy.pool.lock:
                    response, result = self.connection.strategy.pool._incoming.pop(counter)
                    break
            except KeyError:
                pass

            try:
                response_counter, response, result = self.pool.response_queue.get(True, RESPONSE_SLEEPTIME)
                if counter == response_counter:  # response returned
                    break
                else:
                    with self.connection.strategy.pool.lock:
                        self.connection.strategy._incoming[counter] == (response, result)  # stores response in _incoming
                    continue
            except Empty:
                timeout -= RESPONSE_SLEEPTIME

        if isinstance(response, LDAPOperationResult):
            raise response  # an exception has been raised with raise_connections

        return response, result

    def post_send_single_response(self, counter):
        return counter

    def post_send_search(self, counter):
        return counter
