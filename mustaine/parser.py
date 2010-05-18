import datetime
from struct import unpack

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

from mustaine.protocol import *

# Implementation of Hessian 1.0.2 deserialization
#   see: http://hessian.caucho.com/doc/hessian-1.0-spec.xtp

class ParseError(Exception):
    pass

class Parser(object):
    def parse_string(self, string):
        if isinstance(string, UnicodeType):
            stream = StringIO(string.encode('utf-8'))
        else:
            stream = StringIO(string)

        return self.parse_stream(stream)

    def parse_stream(self, stream):
        self._refs   = []
        self._result = None

        if hasattr(stream, 'read') and hasattr(stream.read, '__call__'):
            self._stream = stream
        else:
            raise TypeError('Stream parser can only handle objects supporting read()')

        while True:
            code = self._read(1)

            if   code == 'c':
                if self._result:
                    raise ParseError('Encountered duplicate type header')

                version = self._read(2)
                if version != '\x01\x00':
                    raise ParseError("Encountered unrecognized call version {0!r}".format(version))

                self._result = Call()
                continue

            elif code == 'r':
                if self._result:
                    raise ParseError('Encountered duplicate type header')

                version = self._read(2)
                if version != '\x01\x00':
                    raise ParseError("Encountered unrecognized reply version {0!r}".format(version))

                self._result = Reply()
                continue

            else:
                if not self._result:
                    raise ParseError("Invalid Hessian message marker: {0!r}".format(code))

                if   code == 'H':
                    key, value = self._read_keyval()
                    self._result.headers[key] = value
                    continue

                elif code == 'm':
                    if not isinstance(self._result, Call):
                        raise ParseError('Encountered illegal method name within reply')

                    if self._result.method:
                        raise ParseError('Encountered duplicate method name definition')

                    self._result.method = self._read(unpack('>H', self._read(2))[0])
                    continue

                elif code == 'f':
                    if not isinstance(self._result, Reply):
                        raise ParseError('Encountered illegal fault within call')

                    if self._result.value:
                        raise ParseError('Encountered illegal extra object within reply')

                    self._result.value = self._read_fault()
                    continue

                elif code == 'z':
                    break

                else:
                    if isinstance(self._result, Call):
                        self._result.args.append(self._read_object(code))
                    else:
                        if self._result.value:
                            raise ParseError('Encountered illegal extra object within reply')

                        self._result.value = self._read_object(code)

        # have to hit a 'z' to land here, TODO derefs?
        return self._result


    def _read(self, n):
        try:
            r = self._stream.read(n)
        except IOError:
            raise ParseError('Encountered unexpected end of stream')
        except:
            raise
        else:
            if len(r) == 0:
                raise ParseError('Encountered unexpected end of stream')

        return r

    def _read_object(self, code):
        if   code == 'N':
            return None
        elif code == 'T':
            return True
        elif code == 'F':
            return False
        elif code == 'I':
            return int(unpack('>l', self._read(4))[0])
        elif code == 'L':
            return long(unpack('>q', self._read(8))[0])
        elif code == 'D':
            return float(unpack('>d', self._read(8))[0])
        elif code == 'd':
            return self._read_date()
        elif code == 's' or code == 'x':
            fragment = self._read_string() 
            next     = self._read(1)
            if next.lower() == code:
                return fragment + self._read_object(next)
            else:
                raise ParseError("Expected terminal string segment, got {0!r}".format(next))
        elif code == 'S' or code == 'X':
            return self._read_string()
        elif code == 'b':
            fragment = self._read_binary()
            next     = self._read(1)
            if next.lower() == code:
                return fragment + self._read_binary(next)
        elif code == 'B':
            return self._read_binary()
        elif code == 'r':
            return self._read_remote()
        elif code == 'R':
            # TODO: reference parsing
            raise NotImplementedError("Reference parsing not yet supported")
        elif code == 'V':
            return self._read_list()
        elif code == 'M':
            return self._read_map()
        else:
            raise ParseError("Unknown type marker {0!r}".format(code))

    def _read_date(self):
        timestamp = unpack('>q', self._read(8))[0]
        return datetime.datetime.fromtimestamp(timestamp / 1000) 
    
    def _read_string(self):
        len = unpack('>H', self._read(2))[0]
        
        bytes = []
        while len > 0:
            byte = self._read(1)
            if ord(byte) in range(0x00, 0x7F):
                bytes.append(byte)
            elif ord(byte) in range(0xC2, 0xDF):
                bytes.append(byte + self._read(1))
            elif ord(byte) in range(0xE0, 0xEF):
                bytes.append(byte + self._read(2))
            elif ord(byte) in range(0xF0, 0xF4):
                bytes.append(byte + self._read(3))
            len -= 1
        
        return ''.join(bytes).decode('utf-8')

    def _read_binary(self):
        len = unpack('>H', self._read(2))[0]
        return Binary(self._read(len))

    def _read_remote(self):
        r    = Remote()
        code = self._read(1)

        if code == 't':
            r.type = self._read(unpack('>H', self._read(2))[0])
            code   = self._read(1)
        else:
            r.type = None

        if code != 's' and code != 'S':
            raise ParseError("Expected string object while parsing Remote object URL")

        r.url = self._read_object(code)
        return r

    def _read_reference(self):
        pass # not yet

    def _read_list(self):
        cast = list
        code = self._read(1)

        if code == 't':
            # Python doesn't natively support typed lists, so unless we get a feature request
            # to implement a non-native typed vector, we're going to silently discard the type
            self._read(unpack('>H', self._read(2))[0])
            code = self._read(1)

        if code == 'l':
            # A length was sent with the list, so we'll deserialize this as a tuple. However,
            # the length is irrelevant for decoding so we'll discard it.
            self._read(4)
            code = self._read(1)
            cast = tuple

        members = list()
        while code != 'z':
            members.append(self._read_object(code))
            code = self._read(1)

        return cast(members)

    def _read_map(self):
        cast = None
        code = self._read(1)

        if code == 't':
            # a typed map deserializes to an object rather than a dict
            cast = self._read(unpack('>H', self._read(2))[0])
            code = self._read(1)
        
        fields = dict()
        while code != 'z':
            key, value  = self._read_keyval(code)

            if cast:
                fields[str(key)] = value
            else:
                fields[key] = value

            code = self._read(1)

        if cast:
            return Magic(cast, **fields)
        else:
            return fields

    def _read_fault(self):
        f = Fault()
        for _ in range(3):
            key, value = self._read_keyval()
            setattr(f, key, value)
        return f

    def _read_keyval(self, first=None):
        key   = self._read_object(first or self._read(1))
        value = self._read_object(self._read(1))

        return key, value

