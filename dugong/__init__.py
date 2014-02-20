'''
dugong.py - Python HTTP Client Module

Copyright (C) Nikolaus Rath <Nikolaus@rath.org>

This module may be distributed under the terms of the Python Software Foundation
License Version 2.

The CaseInsensitiveDict implementation is copyright 2013 Kenneth Reitz and
licensed under the Apache License, Version 2.0
(http://www.apache.org/licenses/LICENSE-2.0)
'''

import socket
import logging
import errno
import ssl
import hashlib
from inspect import getdoc
import textwrap
from base64 import b64encode
from collections import deque
from collections.abc import MutableMapping, Mapping
import email
import email.policy
from http.client import (HTTPS_PORT, HTTP_PORT, NO_CONTENT, NOT_MODIFIED)
from select import select, EPOLLIN, EPOLLOUT

__version__ = '1.0'

log = logging.getLogger(__name__)

#: Internal buffer size
BUFFER_SIZE = 64*1024

#: Maximal length of HTTP status line. If the server sends a line longer than
#: this value, `InvalidResponse` will be raised.
MAX_LINE_SIZE = BUFFER_SIZE-1

#: Maximal length of a response header (i.e., for all header
#: lines together). If the server sends a header segment longer than
#: this value, `InvalidResponse` will be raised.
MAX_HEADER_SIZE = BUFFER_SIZE-1

CHUNKED_ENCODING = 'chunked_encoding'
IDENTITY_ENCODING = 'identity_encoding'

#: Marker object for request body size when we're waiting
#: for a 100-continue response from the server
WAITING_FOR_100c = object()


class PollNeeded(tuple):
    '''
    This class encapsulates the requirements for a IO operation to continue.
    `PollNeeded` instances are typically yielded by coroutines.
    '''
    
    __slots__ = ()
    
    def __new__(self, fd, mask):
        return tuple.__new__(self, (fd, mask))

    @property
    def fd(self):
        '''File descriptor that the IO operation depends on'''
        
        return self[0]

    @property
    def mask(self):
        '''Event mask specifiying the type of required IO

        This attribute defines what type of IO the provider of the `PollNeeded`
        instance needs to perform on *fd*. It is expected that, when *fd* is
        ready for IO of the specified type, operation will continue without
        blocking.

        The type of IO is specified as a :ref:`epoll <epoll-objects>` compatible
        event mask, i.e. a bitwise combination of `!select.EPOLLIN` and
        `!select.EPOLLOUT`.
        '''
        
        return self[1]

    def poll(self, timeout=None):
        '''Wait until fd is ready for requested IO

        This is a convenince function that uses `~select.select` to wait until
        `.fd` is ready for requested type of IO.

        If *timeout* is specified, return `False` if the timeout is exceeded
        without the file descriptor becoming ready.
        '''

        read_fds = (self.fd,) if self.mask & EPOLLIN else ()
        write_fds = (self.fd,) if self.mask & EPOLLOUT else ()

        log.debug('calling select with %s, %s', read_fds, write_fds)
        if timeout is None:
            (read_fds, write_fds, _) =  select(read_fds, write_fds, ())
        else:
            (read_fds, write_fds, _) =  select(read_fds, write_fds, (), timeout)

        return bool(read_fds) or bool(write_fds)

    
class HTTPResponse:
    '''
    This class encapsulates information about HTTP response.  Instances of this
    class are returned by the `HTTPConnection.read_response` method and have
    access to response status, reason, and headers.  Response body data
    has to be read directly from the `HTTPConnection` instance.
    '''

    def __init__(self, method, path, status, reason, headers,
                 length=None):

        #: HTTP Method of the request this was response is associated with
        self.method = method

        #: Path of the request this was response is associated with
        self.path = path

        #: HTTP status code returned by the server
        self.status = status

        #: HTTP reason phrase returned by the server
        self.reason = reason

        #: HTTP Response headers, a `email.message.Message` instance
        self.headers = headers

        #: Length of the response body or `None`, if not known
        self.length = length


class BodyFollowing:
    '''
    Sentinel class for the *body* parameter of the
    `~HTTPConnection.send_request` method. Passing an instance of this class
    declares that body data is going to be provided in separate method calls.

    If no length is specified in the constructor, the body data will be send
    using chunked encoding.
    '''

    __slots__ = 'length'

    def __init__(self, length=None):
        #: the length of the body data that is going to be send, or `None`
        #: to use chunked encoding.
        self.length = length

        
class _ChunkTooLong(Exception):
    '''
    Raised by `_co_readstr_until` if the requested end pattern
    cannot be found within the specified byte limit.
    '''
    
    pass
    

class _GeneralError(Exception):
    msg = 'General HTTP Error'

    def __init__(self, msg=None):
        if msg:
            self.msg = msg

    def __str__(self):
        return self.msg


class StateError(_GeneralError):
    '''
    Raised when attempting an operation that doesn't make
    sense in the current connection state.
    '''

    msg = 'Operation invalid in current connection state'


class ExcessBodyData(_GeneralError):
    '''
    Raised when trying to send more data to the server than
    announced.
    '''

    msg = 'Cannot send larger request body than announced'

    
class InvalidResponse(_GeneralError):
    '''
    Raised if the server produced an invalid response (i.e, something
    that is not proper HTTP 1.0 or 1.1).
    '''

    msg = 'Server sent invalid response'


class UnsupportedResponse(_GeneralError):
    '''
    This exception is raised if the server produced a response that is not
    supported. This should not happen for servers that are HTTP 1.1 compatible.

    If an `UnsupportedResponse` exception has been raised, this typically means
    that synchronization with the server will be lost (i.e., dugong cannot
    determine where the current response ends and the next response starts), so
    the connection needs to be reset by calling the
    :meth:`~HTTPConnection.disconnect` method.
    '''

    msg = 'Server sent unsupported response'


class ConnectionClosed(_GeneralError):
    '''
    Raised if the server unexpectedly closed the connection.
    '''

    msg = 'connection closed unexpectedly'

    
class _Buffer:
    '''
    This class represents a buffer with a fixed size, but varying
    fill level.
    '''
    
    __slots__ = ('d', 'b', 'e')

    def __init__(self, size):

        #: Holds the actual data
        self.d = bytearray(size)

        #: Position of the first buffered byte that has not yet
        #: been consumed ("*b*eginning")
        self.b = 0

        #: Fill-level of the buffer ("*e*nd")
        self.e = 0

    def __len__(self):
        '''Return amount of data ready for consumption'''
        return self.e - self.b

    def clear(self):
        '''Forget all buffered data'''
        
        self.b = 0
        self.e = 0

    def compact(self):
        '''Ensure that buffer can be filled up to its maximum size

        If part of the buffer data has been consumed, the unconsumed part is
        copied to the beginning of the buffer to maximize the available space.
        '''
        
        if self.b == 0:
            return

        log.debug('compacting buffer')
        buf = memoryview(self.d)[self.b:self.e]
        len_ = len(buf)
        self.d = bytearray(len(self.d))
        self.d[:len_] = buf
        self.b = 0
        self.e = len_

    def exhaust(self):
        '''Return (and consume) all available data'''

        if self.b == 0:
            log.debug('exhausting buffer (truncating)')
            # Return existing buffer after truncating it
            buf = self.d
            self.d = bytearray(len(self.d))
            buf[self.e:] = b''
        else:
            log.debug('exhausting buffer (copying)')
            buf = self.d[self.b:self.e]
            
        self.b = 0
        self.e = 0
        
        return buf

    
class HTTPConnection:
    '''
    This class encapsulates a HTTP connection. Methods whose name begin with
    ``co_`` return coroutines. Instead of blocking, a coroutines will yield
    a `PollNeeded` instance that encapsulates information about the IO operation
    that would block. The coroutine should be resumed once the operation can be
    performed without blocking.
    '''

    def __init__(self, hostname, port=None, ssl_context=None, proxy=None):

        if port is None:
            if ssl_context is None:
                self.port = HTTP_PORT
            else:
                self.port = HTTPS_PORT
        else:
            self.port = port

        self.ssl_context = ssl_context
        self.hostname = hostname

        #: Socket object connecting to the server
        self._sock = None

        #: Read-buffer
        self._rbuf = _Buffer(BUFFER_SIZE)

        #: a tuple ``(hostname, port)`` of the proxy server to use or `None`.
        #: Note that currently only CONNECT-style proxying is supported.
        self.proxy = proxy

        #: a deque of ``(method, path, body_len)`` tuples corresponding to
        #: requests whose response has not yet been read completely. Requests
        #: with Expect: 100-continue will be added twice to this queue, once
        #: after the request header has been sent, and once after the request
        #: body data has been sent. *body_len* is `None`, or the size of the
        #: **request** body that still has to be sent when using 100-continue.
        self._pending_requests = deque()

        #: This attribute is `None` when a request has been sent completely.  If
        #: request headers have been sent, but request body data is still
        #: pending, it is set to a ``(method, path, body_len)`` tuple. *body_len*
        #: is the number of bytes that that still need to send, or
        #: WAITING_FOR_100c if we are waiting for a 100 response from the server.
        self._out_remaining = None

        #: Number of remaining bytes of the current response body (or current
        #: chunk), or `None` if the response header has not yet been read.
        self._in_remaining = None

        #: Transfer encoding of the active response (if any).
        self._encoding = None

    # Implement bare-bones `io.BaseIO` interface, so that instances
    # can be wrapped in `io.TextIOWrapper` if desired.
    def writable(self):
        return True
    def readable(self):
        return True
    def seekable(self):
        return False

    # We consider the stream closed if there is no active response
    # from which body data could be read.
    @property
    def closed(self):
        return self._in_remaining is None
        
    def connect(self):
        """Connect to the remote server

        This method generally does not need to be called manually.
        """

        log.debug('start')
        
        if self.proxy:
            log.debug('connecting to %s', self.proxy)
            self._sock = socket.create_connection(self.proxy)
            eval_coroutine(self._co_tunnel())
        else:
            log.debug('connecting to %s', (self.hostname, self.port))
            self._sock = socket.create_connection((self.hostname, self.port))

        if self.ssl_context:
            log.debug('establishing ssl layer')
            server_hostname = self.hostname if ssl.HAS_SNI else None
            self._sock = self.ssl_context.wrap_socket(self._sock, server_hostname=server_hostname)

            try:
                ssl.match_hostname(self._sock.getpeercert(), self.hostname)
            except:
                self.close()
                raise

        self._sock.setblocking(False)
        self._rbuf.clear()
        self._out_remaining = None
        self._in_remaining = None
        self._pending_requests = deque()

        log.debug('done')
        
    def _co_tunnel(self):
        '''Set up CONNECT tunnel to destination server'''

        log.debug('start connecting to %s:%d', self.hostname, self.port)
        
        yield from self._co_send(("CONNECT %s:%d HTTP/1.0\r\n\r\n"
                                  % (self.hostname, self.port)).encode('latin1'))

        (status, reason) = yield from self._co_read_status()
        log.debug('got %03d %s', status, reason)
        yield from self._co_read_header()

        if status != 200:
            self.disconnect()
            raise ConnectionError("Tunnel connection failed: %d %s" % (status, reason))

    def get_ssl_peercert(self, binary_form=False):
        '''Get peer SSL certificate

        If plain HTTP is used, return `None`. Otherwise, the call is delegated
        to the underlying SSL sockets `~ssl.SSLSocket.getpeercert` method.
        '''

        if not self.ssl_context:
            return None
        else:
            if not self._sock:
                self.connect()
            return self._sock.getpeercert()

    def get_ssl_cipher(self):
        '''Get active SSL cipher

        If plain HTTP is used, return `None`. Otherwise, the call is delegated
        to the underlying SSL sockets `~ssl.SSLSocket.cipher` method.
        '''

        if not self.ssl_context:
            return None
        else:
            if not self._sock:
                self.connect()
            return self._sock.cipher()
        
    def send_request(self, method, path, headers=None, body=None, expect100=False):
        '''placeholder, will be replaced dynamically'''
        eval_coroutine(self.co_send_request(method, path, headers=headers,
                                            body=body, expect100=expect100))
        
    def co_send_request(self, method, path, headers=None, body=None, expect100=False):
        '''Send a new HTTP request to the server

        The message body may be passed in the *body* argument or be sent
        separately. In the former case, *body* must be a :term:`bytes-like
        object`. In the latter case, *body* must be an a `BodyFollowing`
        instance specifying the length of the data that will be sent. If no
        length is specified, the data will be send using chunked encoding.

        *headers* should be a mapping containing the HTTP headers to be send
        with the request. Multiple header lines with the same key are not
        supported. It is recommended to pass a `CaseInsensitiveDict` instance,
        other mappings will be converted to `CaseInsensitiveDict` automatically.

        If *body* is a provided as a :term:`bytes-like object`, a
        ``Content-MD5`` header is generated automatically unless it has been
        provided in *headers* already.
        '''

        log.debug('start')
        
        if expect100 and not isinstance(body, BodyFollowing):
            raise ValueError('expect100 only allowed for separate body')

        if self._sock is None:
            self.connect()

        if self._out_remaining:
            raise StateError('body data has not been sent completely yet')

        if headers is None:
            headers = CaseInsensitiveDict()
        elif not isinstance(headers, CaseInsensitiveDict):
            headers = CaseInsensitiveDict(headers)

        pending_body_size = None
        if body is None:
            headers['Content-Length'] = '0'
        elif isinstance(body, BodyFollowing):
            if body.length is None:
                raise ValueError('Chunked encoding not yet supported.')
            log.debug('preparing to send %d bytes of body data', body.length)
            if expect100:
                headers['Expect'] = '100-continue'
                # Do not set _out_remaining, we must only send data once we've
                # read the response. Instead, save body size in
                # _pending_requests so that it can be restored by
                # read_response().
                pending_body_size = body.length
                self._out_remaining = (method, path, WAITING_FOR_100c)
            else:
                self._out_remaining = (method, path, body.length)
            headers['Content-Length'] = str(body.length)
            body = None
        elif isinstance(body, (bytes, bytearray, memoryview)):
            headers['Content-Length'] = str(len(body))
            if 'Content-MD5' not in headers:
                log.debug('computing content-md5')
                headers['Content-MD5'] = b64encode(hashlib.md5(body).digest()).decode('ascii')
        else:
            raise TypeError('*body* must be None, bytes-like or BodyFollowing')

        # Generate host header
        host = self.hostname
        if host.find(':') >= 0:
            host = '[{}]'.format(host)
        default_port = HTTPS_PORT if self.ssl_context else HTTP_PORT
        if self.port == default_port:
            headers['Host'] = host
        else:
            headers['Host'] = '{}:{}'.format(host, self.port)

        # Assemble request
        headers['Accept-Encoding'] = 'identity'
        if 'Connection' not in headers:
            headers['Connection'] = 'keep-alive'
        request = [ '{} {} HTTP/1.1'.format(method, path).encode('latin1') ]
        for key, val in headers.items():
            request.append('{}: {}'.format(key, val).encode('latin1'))
        request.append(b'')

        if body is not None:
            request.append(body)
        else:
            request.append(b'')

        buf = b'\r\n'.join(request)

        log.debug('sending %s %s', method, path)
        yield from self._co_send(buf)
        if not self._out_remaining or expect100:
            self._pending_requests.append((method, path, pending_body_size))

    def _co_send(self, buf):
        '''Send *buf* to server'''

        log.debug('trying to send %d bytes', len(buf))
        
        if not isinstance(buf, memoryview):
            buf = memoryview(buf)

        fd = self._sock.fileno()
        while True:
            if not select((), (fd,), (), 0)[1]:
                log.debug('yielding')
                yield PollNeeded(fd, EPOLLOUT)
                continue

            try:
                len_ = self._sock.send(buf)
            except BrokenPipeError:
                raise ConnectionClosed('found closed when trying to write')
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    # Blackhole routing, according to ip(7)
                    raise ConnectionClosed('ip route goes into black hole')
                else:
                    raise
            except InterruptedError:
                # According to send(2), this means that no data has been sent
                # at all before the interruption, so we just try again.
                pass
            log.debug('sent %d bytes', len_)
            buf = buf[len_:]
            if len(buf) == 0:
                log.debug('done')
                return
            
    def write(self, buf):
        '''placeholder, will be replaced dynamically'''
        eval_coroutine(self.co_write(buf))

    def co_write(self, buf):
        '''Write request body data

        `ExcessBodyData` will be raised when attempting to send more data than
        required to complete the request body of the active request.
        '''

        log.debug('start (len=%d)', len(buf))
        
        if not self._out_remaining:
            raise StateError('No active request with pending body data')

        (method, path, remaining) = self._out_remaining
        if remaining is WAITING_FOR_100c:
            raise StateError("can't write when waiting for 100-continue")

        if len(buf) > remaining:
            raise ExcessBodyData('trying to write %d bytes, but only %d bytes pending'
                                    % (len(buf), remaining))

        yield from self._co_send(buf)

        len_ = len(buf)
        if len_ == remaining:
            log.debug('body sent fully')
            self._out_remaining = None
            self._pending_requests.append((method, path, None))
        else:
            self._out_remaining = (method, path, remaining - len_)

        log.debug('done')

    def response_pending(self):
        '''Return `True` if there are still outstanding responses

        This includes responses that have been partially read.
        '''

        return len(self._pending_requests) > 0

    def read_response(self):
        '''placeholder, will be replaced dynamically'''
        return eval_coroutine(self.co_read_response())

    def co_read_response(self):
        '''Read response status line and headers

        Return a `HTTPResponse` instance containing information about response
        status, reason, and headers. The response body data must be retrieved
        separately (e.g. using `.read` or `.readall`).

        Even for a response with empty body, one of the body reading method must
        be called once before the next response can be processed.
        '''

        log.debug('start')
        
        if len(self._pending_requests) == 0:
            raise StateError('No pending requests')

        if self._in_remaining is not None:
            raise StateError('Previous response not read completely')

        (method, path, body_size) = self._pending_requests[0]

        # Need to loop to handle any 1xx responses
        while True:
            (status, reason) = yield from self._co_read_status()
            log.debug('got %03d %s', status, reason)
            
            hstring = yield from self._co_read_header()
            header = email.message_from_string(hstring, policy=email.policy.HTTP)

            if status < 100 or status > 199:
                break

            # We are waiting for 100-continue
            if body_size is not None and status == 100:
                break

        # Handle (expected) 100-continue
        if status == 100:
            assert self._out_remaining == (method, path, WAITING_FOR_100c)

            # We're ready to sent request body now
            self._out_remaining = self._pending_requests.popleft()
            self._in_remaining = None

            # Return early, because we don't have to prepare
            # for reading the response body at this time
            return HTTPResponse(method, path, status, reason, header, length=0)

        # Handle non-100 status when waiting for 100-continue
        elif body_size is not None:
            assert self._out_remaining == (method, path, WAITING_FOR_100c)
            # RFC 2616 actually states that the server MAY continue to read
            # the request body after it has sent a final status code
            # (http://tools.ietf.org/html/rfc2616#section-8.2.3). However,
            # that totally defeats the purpose of 100-continue, so we hope
            # that the server behaves sanely and does not attempt to read
            # the body of a request it has already handled. (As a side note,
            # this ambuigity in the RFC also totally breaks HTTP pipelining,
            # as we can never be sure if the server is going to expect the
            # request or some request body data).
            self._out_remaining = None

        #
        # Prepare to read body
        #
        body_length = None

        tc = header['Transfer-Encoding']
        if tc:
            tc = tc.lower()
        if tc and tc == 'chunked':
            log.debug('Chunked encoding detected')
            self._encoding = CHUNKED_ENCODING
            self._in_remaining = 0
        elif tc and tc != 'identity':
            # Server must not sent anything other than identity or chunked, so
            # we raise InvalidResponse rather than UnsupportedResponse. We defer
            # raising the exception to read(), so that we can still return the
            # headers and status (and don't fail if the response body is empty).
            log.warning('Server uses invalid response encoding "%s"', tc)
            self._encoding = InvalidResponse('Cannot handle %s encoding' % tc)
        else:
            log.debug('identity encoding detected')
            self._encoding = IDENTITY_ENCODING

        # does the body have a fixed length? (of zero)
        if (status == NO_CONTENT or status == NOT_MODIFIED or
            100 <= status < 200 or method == 'HEAD'):
            log.debug('no content by RFC')
            body_length = 0
            self._in_remaining = 0
            # for these cases, there isn't even a zero chunk we could read
            self._encoding = IDENTITY_ENCODING

        # Chunked doesn't require content-length
        elif self._encoding is CHUNKED_ENCODING:
            pass

        # Otherwise we require a content-length. We defer raising the exception
        # to read(), so that we can still return the headers and status.
        elif ('Content-Length' not in header
              and not isinstance(self._encoding, InvalidResponse)):
            log.debug('no content length and no chunkend encoding, will raise on read')
            self._encoding = UnsupportedResponse('No content-length and no chunked encoding')
            self._in_remaining = 0

        else:
            self._in_remaining = int(header['Content-Length'])
            body_length = self._in_remaining

        log.debug('done (in_remaining=%d)', self._in_remaining)

        return HTTPResponse(method, path, status, reason, header, body_length)
    
    def _co_read_status(self):
        '''Read response line'''

        log.debug('start')

        # read status
        try:
            line = yield from self._co_readstr_until(b'\r\n', MAX_LINE_SIZE)
        except _ChunkTooLong:
            raise InvalidResponse('server send ridicously long status line')

        try:
            version, status, reason = line.split(None, 2)
        except ValueError:
            try:
                version, status = line.split(None, 1)
                reason = ""
            except ValueError:
                # empty version will cause next test to fail.
                version = ""

        if not version.startswith("HTTP/1"):
            raise UnsupportedResponse('%s not supported' % version)

        # The status code is a three-digit number
        try:
            status = int(status)
            if status < 100 or status > 999:
                raise InvalidResponse('%d is not a valid status' % status)
        except ValueError:
            raise InvalidResponse('%s is not a valid status' % status)

        log.debug('done')
        return (status, reason.strip())
    
    def _co_read_header(self):
        '''Read response header'''

        log.debug('start')

        # Peek into buffer. If the first characters are \r\n, then the header 
        # is empty (so our search for \r\n\r\n would fail)
        rbuf = self._rbuf
        if len(rbuf) < 2:
            yield from self._co_fill_buffer(2)
        if rbuf.d[rbuf.b:rbuf.b+2] == b'\r\n':
            log.debug('done (empty header)')
            rbuf.b += 2
            return ''
            
        try:
            hstring = yield from self._co_readstr_until(b'\r\n\r\n', MAX_HEADER_SIZE)
        except _ChunkTooLong:
            raise InvalidResponse('server sent ridicously long header')

        log.debug('done (%d characters)', len(hstring))
        return hstring

    def read(self, len_=None):
        '''placeholder, will be replaced dynamically'''
        if len_ is None:
            return self.readall()
        return eval_coroutine(self.co_read(len_))

    def co_read(self, len_=None):
        '''Read up to *len_* bytes of response body data

        This method may return less than *len_* bytes, but will return ``b''`` only
        if the response body has been read completely. Further attempts to read
        more data after ``b''`` has been returned will result in `StateError` being
        raised.

        If *len_* is `None`, this method returns the entire response body. Further
        calls will not return ``b''`` but directly raise `StateError`.
        '''

        log.debug('start (len=%d)', len_)
        
        if len_ is None:
            return (yield from self.co_readall())

        if len_ == 0:
            return b''

        if self._in_remaining is None:
            raise StateError('No active response with body')

        if self._encoding is IDENTITY_ENCODING:
            return (yield from self._co_read_id(len_))
        elif self._encoding is CHUNKED_ENCODING:
            return (yield from self._co_read_chunked(len_=len_))
        elif isinstance(self._encoding, Exception):
            raise self._encoding
        else:
            raise RuntimeError('ooops, this should not be possible')

    def readinto(self, buf):
        '''placeholder, will be replaced dynamically'''
        return eval_coroutine(self.co_readinto(buf))
 
    def co_readinto(self, buf):
        '''Read response body data into *buf*

        Return the number of bytes written or zero if the response body has been
        read completely. Further attempts to read more data after zero has been
        returned will result in `StateError` being raised.

        *buf* must implement the memoryview protocol.
        '''

        log.debug('start (buflen=%d)', len(buf))
        
        if len(buf) == 0:
            return 0

        if self._in_remaining is None:
            raise StateError('No active response with body')

        if self._encoding is IDENTITY_ENCODING:
            return (yield from self._co_readinto_id(buf))
        elif self._encoding is CHUNKED_ENCODING:
            return (yield from self._co_read_chunked(buf=buf))
        elif isinstance(self._encoding, Exception):
            raise self._encoding
        else:
            raise RuntimeError('ooops, this should not be possible')

    def _co_read_id(self, len_):
        '''Read up to *len* bytes of response body assuming identity encoding'''

        log.debug('start (len=%d)', len_)
        assert self._in_remaining is not None
        
        if not self._in_remaining:
            # Body retrieved completely, clean up
            self._in_remaining = None
            self._pending_requests.popleft()
            return b''

        sock_fd = self._sock.fileno()
        rbuf = self._rbuf
        len_ = min(len_, self._in_remaining)
        log.debug('updated len_=%d', len_)
        
        # Loop while we could return more data than we have buffered
        # and buffer is not full
        log.debug('len(rbuf)=%d, len=%d, rbuf.e=%d, len(rbuf.d)=%d',
                  len(rbuf), len_, rbuf.e, len(rbuf.d))
        while len(rbuf) < len_ and rbuf.e < len(rbuf.d):
            got_data = self._try_fill_buffer()
            if not got_data and not rbuf:
                log.debug('buffer empty and nothing to read, yielding..')
                yield PollNeeded(sock_fd, EPOLLIN)
            elif not got_data:
                log.debug('nothing more to read')
                break

        len_ = min(len_, len(rbuf))
        self._in_remaining -= len_

        if len_ < len(rbuf):
            buf = rbuf.d[rbuf.b:rbuf.b+len_]
            rbuf.b += len_
        else:
            buf = rbuf.exhaust()

        log.debug('done (%d bytes)', len(buf))
        return buf

    def _co_readinto_id(self, buf):
        '''Read response body into *buf* assuming identity encoding'''

        log.debug('start (buflen=%d)', len(buf))
        
        assert self._in_remaining is not None
        if not self._in_remaining:
            # Body retrieved completely, clean up
            self._in_remaining = None
            self._pending_requests.popleft()
            return 0

        sock_fd = self._sock.fileno()
        rbuf = self._rbuf
        if not isinstance(buf, memoryview):
            buf = memoryview(buf)
        len_ = min(len(buf), self._in_remaining)
        log.debug('updated len_=%d', len_)

        # First use read buffer contents
        pos = min(len(rbuf), len_)
        if pos:
            log.debug('using buffered data')
            buf[:pos] = rbuf.d[rbuf.b:rbuf.b+pos]
            rbuf.b += pos
            if rbuf.b == rbuf.e:
                rbuf.b = 0
                rbuf.e = 0
            self._in_remaining -= pos
            
            # If we've read enough, return immediately
            if pos == len_:
                log.debug('done (got all we need, %d bytes)', pos)
                return pos

            # Otherwise, prepare to read more from socket
            log.debug('got %d bytes from buffer', pos)
            assert not len(rbuf)

        while True:
            log.debug('trying to read from socket')
            try:
                read = self._sock.recv_into(buf[pos:len_])
            except (socket.timeout, ssl.SSLWantReadError, BlockingIOError):
                if pos:
                    log.debug('done (nothing more to read, got %d bytes)', pos)
                    return pos
                else:
                    log.debug('no data yet and nothing to read, yielding..')
                    yield PollNeeded(sock_fd, EPOLLIN)
                    continue

            if not read:
                raise ConnectionClosed('connection closed unexpectedly')
            
            log.debug('got %d bytes', read)
            self._in_remaining -= read
            pos += read
            if pos == len_:
                log.debug('done (got all we need, %d bytes)', pos)
                return pos

    def _co_read_chunked(self, len_=None, buf=None):
        '''Read response body assuming chunked encoding

        If *len_* is not `None`, reads up to *len_* bytes of data and returns
        a `bytes-like object`. If *buf* is not `None`, reads data into *buf*.
        '''

        # TODO: In readinto mode, we always need an extra sock.recv()
        # to get the chunk trailer.. is there some way to avoid that? And
        # maybe also put the beginning of the next chunk into the read buffer right away?

        log.debug('start (%s mode)', 'readinto' if buf else 'read')
        assert (len_ is None) != (buf is None)
        assert bool(len_) or bool(buf)
        assert self._in_remaining is not None
        
        if self._in_remaining == 0:
            log.debug('starting next chunk')
            try:
                line = yield from self._co_readstr_until(b'\r\n', MAX_LINE_SIZE)
            except _ChunkTooLong:
                raise InvalidResponse('could not find next chunk marker')

            i = line.find(";")
            if i >= 0:
                log.debug('stripping chunk extensions: %s', line[i:])
                line = line[:i] # strip chunk-extensions
            try:
                self._in_remaining = int(line, 16)
            except ValueError:
                raise InvalidResponse('Cannot read chunk size %r' % line[:20])

            log.debug('chunk size is %d', self._in_remaining)
            if self._in_remaining == 0:
                self._in_remaining = None
                self._pending_requests.popleft()

        if self._in_remaining is None:
            res = 0 if buf else b''
        elif buf:
            res = yield from self._co_readinto_id(buf)
        else:
            res = yield from self._co_read_id(len_)

        if not self._in_remaining:
            log.debug('chunk complete')
            yield from self._co_read_header()

        log.debug('done')
        return res
    
    def _co_readstr_until(self, substr, maxsize):
        '''Read from server until *substr*, and decode to latin1

        If *substr* cannot be found in the next *maxsize* bytes,
        raises `_ChunkTooLong`.
        '''

        if not isinstance(substr, (bytes, bytearray, memoryview)):
            raise TypeError('*substr* must be bytes-like')

        log.debug('reading until %s', substr)
        
        sock_fd = self._sock.fileno()
        rbuf = self._rbuf
        sub_len = len(substr)

        # Make sure that substr cannot be split over more than one part
        assert len(rbuf.d) > sub_len
        
        parts = []
        while True:
            # substr may be split between last part and current buffer
            # This isn't very performant, but it should be pretty rare
            if parts and sub_len > 1:
                buf = _join((parts[-1][-sub_len:],
                            rbuf.d[rbuf.b:min(rbuf.e, rbuf.b+sub_len-1)]))
                idx = buf.find(substr)
                if idx >= 0:
                    idx -= sub_len
                    break
                
            #log.debug('rbuf is: %s', rbuf.d[rbuf.b:min(rbuf.e, rbuf.b+512)])
            stop = min(rbuf.e, rbuf.b + maxsize)
            idx = rbuf.d.find(substr, rbuf.b, stop)
            
            if idx >= 0: # found
                break
            if stop != rbuf.e:
                raise _ChunkTooLong()

            # If buffer is full, store away the part that we need for sure
            if rbuf.e == len(rbuf.d):
                log.debug('buffer is full, storing part')
                buf = rbuf.exhaust()
                parts.append(buf)
                maxsize -= len(buf)

            # Refill buffer
            while not self._try_fill_buffer():
                log.debug('need more data, yielding')
                yield PollNeeded(sock_fd, EPOLLIN)

        log.debug('found substr at %d', idx)
        idx += len(substr)
        buf = rbuf.d[rbuf.b:idx]
        rbuf.b = idx

        if parts:
            parts.append(buf)
            buf = _join(parts)
            
        try:
            return buf.decode('latin1')
        except UnicodeDecodeError:
            raise InvalidResponse('server response cannot be decoded to latin1')

    def _try_fill_buffer(self):
        '''Try to fill up read buffer

        Returns the number of bytes read into buffer, or `None` if no
        data was available on the socket. May raise `ConnectionClosed`.
        '''

        log.debug('start')
        rbuf = self._rbuf
        try:
            len_ = self._sock.recv_into(memoryview(rbuf.d)[rbuf.e:])
        except (socket.timeout, ssl.SSLWantReadError, BlockingIOError):
            log.debug('done (nothing ready)')
            return None

        if not len_:
            assert rbuf.e < len(rbuf.d)
            raise ConnectionClosed('connection closed unexpectedly')

        rbuf.e += len_
        log.debug('done (got %d bytes)', len_)
        return len_

    def _co_fill_buffer(self, len_):
        '''Make sure that there are at least *len_* bytes in buffer'''

        rbuf = self._rbuf
        sock_fd = self._sock.fileno()
        while len(rbuf) < len_:
            if len(rbuf.d) - rbuf.b < len_:
                self._rbuf.compact()
            if not self._try_fill_buffer():
                yield PollNeeded(sock_fd, EPOLLIN)

    def readall(self):
        '''placeholder, will be replaced dynamically'''
        return eval_coroutine(self.co_readall())

    def co_readall(self):
        '''Read and return complete response body

        After this function has returned, attemps to read more body data
        for the same response will raise `StateError`.
        '''

        log.debug('start')
        parts = []
        while True:
            buf = yield from self.co_read(BUFFER_SIZE)
            log.debug('got %d bytes', len(buf))
            if not buf:
                break
            parts.append(buf)

            
        buf = _join(parts)
        log.debug('done (%d bytes)', len(buf))
        return buf

    def discard(self):
        '''placeholder, will be replaced dynamically'''
        return eval_coroutine(self.co_discard())
    
    def co_discard(self):
        '''Read and discard current response body

        After this function has returned, attempts to read more body data
        for the same response will raise `StateError`.
        '''

        log.debug('start')
        buf = memoryview(bytearray(BUFFER_SIZE))
        while True:
            len_ = yield from self.co_readinto(buf)
            if not len_:
                break
            log.debug('discarding %d bytes', len_)
        log.debug('done')
        
    def disconnect(self):
        '''Close HTTP connection'''

        log.debug('start')
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                # When called to reset after connection problems, socket
                # may have shut down already.
                pass
            self._sock.close()
            self._sock = None
            self._rbuf.clear()
        else:
            log.debug('already closed')


def _extend_HTTPConnection_docstrings():

    co_suffix = '\n\n' + textwrap.fill(
        'This method returns a coroutine. `.%s` is a regular method '
        'implementing the same functionality.', width=78)
    reg_suffix = '\n\n' + textwrap.fill(
        'This method may block. `.co_%s` provides a coroutine '
        'implementing the same functionality without blocking.', width=78)

    for name in ('read', 'read_response', 'readall', 'readinto', 'send_request',
                 'write', 'discard'):
        fn = getattr(HTTPConnection, name)
        cofn = getattr(HTTPConnection, 'co_' + name)

        fn.__doc__ = getdoc(cofn) + reg_suffix % name
        cofn.__doc__ = getdoc(cofn) + co_suffix % name

_extend_HTTPConnection_docstrings()

def _join(parts):
    '''Join a sequence of byte-like objects

    This method is necessary because `bytes.join` does not work with
    memoryviews.
    '''

    size = 0
    for part in parts:
        size += len(parts)

    buf = bytearray(size)
    i = 0
    for part in parts:
        len_ = len(part)
        buf[i:i+len_] = part
        i += len_

    return buf
        
def eval_coroutine(crt):
    '''Evaluate *crt* (polling as needed) and return its result'''

    try:
        while True:
            assert next(crt).poll()
            log.debug('polling')
    except StopIteration as exc:
        return exc.value

def is_temp_network_error(exc):
    '''Return true if *exc* represents a potentially temporary network problem'''

    if isinstance(exc, (socket.timeout, ConnectionError, TimeoutError, InterruptedError,
                        ConnectionClosed, ssl.SSLZeroReturnError, ssl.SSLEOFError,
                        ssl.SSLSyscallError)):
        return True

    # Formally this is a permanent error. However, it may also indicate
    # that there is currently no network connection to the DNS server
    elif (isinstance(exc, (socket.gaierror, socket.herror))
          and exc.errno in (socket.EAI_AGAIN, socket.EAI_NONAME)):
        return True

    return False


class CaseInsensitiveDict(MutableMapping):
    """A case-insensitive `dict`-like object.

    Implements all methods and operations of
    :class:`collections.abc.MutableMapping` as well as `.copy`.

    All keys are expected to be strings. The structure remembers the case of the
    last key to be set, and :meth:`!iter`, :meth:`!keys` and :meth:`!items` will
    contain case-sensitive keys. However, querying and contains testing is case
    insensitive::

        cid = CaseInsensitiveDict()
        cid['Accept'] = 'application/json'
        cid['aCCEPT'] == 'application/json' # True
        list(cid) == ['Accept'] # True

    For example, ``headers['content-encoding']`` will return the value of a
    ``'Content-Encoding'`` response header, regardless of how the header name
    was originally stored.

    If the constructor, :meth:`!update`, or equality comparison operations are
    given multiple keys that have equal lower-case representions, the behavior
    is undefined.
    """

    def __init__(self, data=None, **kwargs):
        self._store = dict()
        if data is None:
            data = {}
        self.update(data, **kwargs)

    def __setitem__(self, key, value):
        # Use the lowercased key for lookups, but store the actual
        # key alongside the value.
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key):
        return self._store[key.lower()][1]

    def __delitem__(self, key):
        del self._store[key.lower()]

    def __iter__(self):
        return (casedkey for casedkey, mappedvalue in self._store.values())

    def __len__(self):
        return len(self._store)

    def lower_items(self):
        """Like :meth:`!items`, but with all lowercase keys."""
        return (
            (lowerkey, keyval[1])
            for (lowerkey, keyval)
            in self._store.items()
        )

    def __eq__(self, other):
        if isinstance(other, Mapping):
            other = CaseInsensitiveDict(other)
        else:
            return NotImplemented
        # Compare insensitively
        return dict(self.lower_items()) == dict(other.lower_items())

    # Copy is required
    def copy(self):
         return CaseInsensitiveDict(self._store.values())

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, dict(self.items()))