#!/usr/bin/env python
#
# PyAuthenNTLM2: A mod-python module for Apache that carries out NTLM authentication
#
# ntlm_proxy.py
#
# Copyright 2011 Legrandin <gooksankoo@hoiptorrow.mailexpire.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import socket
from struct import pack, unpack
from binascii import hexlify, unhexlify

def tuc(s):
    return s.encode('utf-16-le')

class SMB_Parse_Exception(Exception):
    pass

class SMB_Context:
    """This is a class that creates and parses SMB messages belonging to the same context.
    """

    # Direct TCP transport (see 2.1 in MS-SMB)
    Transport_Header_Length         = 4

    # See MS-CIFS and MS-SMB
    SMB_Header_Length               = 32
    SMB_COM_NEGOTIATE               = 0x72
    SMB_COM_SESSION_SETUP_ANDX      = 0x73

    SMB_FLAGS2_EXTENDED_SECURITY    = 0x0800
    SMB_FLAGS2_NT_STATUS            = 0x4000
    SMB_FLAGS2_UNICODE              = 0x8000

    CAP_UNICODE                     = 0x00000004
    CAP_NT_SMBS                     = 0x00000010
    CAP_STATUS32                    = 0x00000040
    CAP_EXTENDED_SECURITY           = 0x80000000

    # ASN.1 DER OID assigned to NTLM
    #   1.3.6.1.4.1.311.2.2.10
    ntlm_oid = '\x06\x0a\x2b\x06\x01\x04\x01\x82\x37\x02\x02\x0a'
    
    def __init__(self):
        self.userId = 0
        self.sessionKey = '\x00'*4
        self.systemTime = 0

    ### Begin ASN.1 DER helpers

    def maketlv(self, dertype, payload):
        """Construct a DER encoding of an ASN.1 entity of given type and payload"""
        if len(payload)<128:
            return dertype + chr(len(payload)) + payload
        if len(payload)<256:
            return dertype + '\x81' + chr(len(payload)) + payload
        return dertype + '\x82' + pack('>H',len(payload)) + payload

    def makeseq(self, payload):
        """Construct a DER encoding of an ASN.1 SEQUENCE of given payload"""
        return self.maketlv('\x30', payload)

    def makeoctstr(self, payload):
        """Construct a DER encoding of an ASN.1 OCTET STRING of given payload"""
        return self.maketlv('\x04', payload)

    def makegenstr(self, payload):
        """Construct a DER encoding of an ASN.1 GeneralString of given payload"""
        return self.maketlv('\x1b', payload)

    def parsetlv(self, dertype, derobj, partial=False):
        """Parse a DER encoded object.
        
        @dertype    The expected type field (class, P/C, tag).
        @derobj     The DER encoded object to parse.
        @partial    Flag indicating whether all bytes should be consumed by the parser.

        An exception is raised if parsing fails, if the type is not matched, or if 'partial'
        is not honoured.

        @return     The object payload if partial is False
                    A list (object payload, remaining data) if partial is True
        """
        if derobj[0]!=dertype:
            raise SMB_Parse_Exception('DER element %s does not start with type %s.' % (hexlify(derobj), hex(ord(tag))))
        
        # Decode DER length
        length = ord(derobj[1])
        if length<128:
            pstart = 2
        else:
            nlength = length & 0x1F
            if nlength==1:
                length = ord(derobj[2])
            elif nlength==2:
                length = unpack('>H', derobj[2:4])[0]
            pstart = 2 + nlength
        if partial:
            if len(derobj)<length+pstart:
                raise SMB_Parse_Exception('DER payload %s is shorter than expected (%d bytes, type %X).' % (hexlify(derobj), length, ord(derobj[0])))
            return derobj[pstart:pstart+length], derobj[pstart+length:]
        if len(derobj)!=length+pstart:
            raise SMB_Parse_Exception('DER payload %s is not %d bytes long (type %X).' % (hexlify(derobj), length, ord(derobj[0])))
        return derobj[pstart:]

    def parseenum(self, payload, partial=False):
        """Parse a DER ENUMERATED
        
        @paylaod    The complete DER object
        @partial    Flag indicating whether all bytes should be consumed by the parser.
        @return     The ENUMERATED value if partial is False
                    A list (ENUMERATED value, remaining data) if partial is True
        """
        res = self.parsetlv('\x0a', payload, partial)
        if partial:
            return (ord(res[0]), res[1])
        else:
            return ord(res[0])

    def parseseq(self, payload, partial=False):
        """Parse a DER SEQUENCE
        
        @paylaod    The complete DER object
        @partial    Flag indicating whether all bytes should be consumed by the parser.
        @return     The SEQUENCE byte string if partial is False
                    A list (SEQUENCE byte string, remaining data) if partial is True
        """
        return self.parsetlv('\x30', payload, partial)


    def parseoctstr(self, payload, partial=False):
        """Parse a DER OCTET STRING
        
        @paylaod    The complete DER object
        @partial    Flag indicating whether all bytes should be consumed by the parser.
        @return     The OCTET STRING byte string if partial is False
                    A list (OCTET STRING byte string, remaining data) if partial is True
        """
        return self.parsetlv('\x04', payload, partial)

    ### End ASN1. DER helpers

    def addTransport(self, msg):
        '''Add Direct TCP transport to SMB message'''
        return '\x00\x00' + pack('>H', len(msg)) + msg

    def getTransportLength(self, msg):
        '''Return length of SMB message from Direct TCP tranport'''
        return unpack('>H', msg[2:4])[0]

    def removeTransport(self, msg):
        '''Remove Direct TCP transport to SMB message'''
        data = msg[4:]
        length = unpack('>H', msg[2:4])[0]
        if msg[0:2]!='\x00\x00' or length!=len(data):
            raise SMB_Parse_Exception('Error while parsing Direct TCP transport Direct (%d, expected %d).' % (length,len(data)))
        return data

    def make_gssapi_token(self, ntlm_token, type1=True):
        '''Construct a GSSAPI/SPNEGO message, wrapping the given NTLM token.
        
        @ntlm_token     The NTLM token to embed into the message
        @type1          True if Type1, False if Type 3
        @return         The GSSAPI/SPNEGO message
        '''

        if not type1:
            mechToken = self.maketlv('\xa2', self.makeoctstr(ntlm_token))
            negTokenResp = self.maketlv('\xa1', self.makeseq(mechToken))
            return negTokenResp

        # NegTokenInit (rfc4178)
        mechlist = self.makeseq(self.ntlm_oid)
        mechTypes = self.maketlv('\xa0', mechlist)
        mechToken = self.maketlv('\xa2', self.makeoctstr(ntlm_token))

        # NegotiationToken (rfc4178)
        negTokenInit = self.makeseq(mechTypes + mechToken ) # + mechListMIC)
        innerContextToken = self.maketlv('\xa0', negTokenInit)

        # MechType + innerContextToken (rfc2743)
        thisMech = '\x06\x06\x2b\x06\x01\x05\x05\x02' # SPNEGO OID 1.3.6.1.5.5.2
        spnego = thisMech + innerContextToken

        # InitialContextToken (rfc2743)
        msg = self.maketlv('\x60', spnego)
        return msg

    def extract_gssapi_token(self, msg):
        '''Extract the NTLM token from a GSSAPI/SPNEGO message.
        
        @msg        The full GSSAPI/SPNEGO message
        @return     The NTLM message
        '''

        # Extract negTokenResp from NegotiationToken
        spnego = self.parseseq(self.parsetlv('\xa1', msg))

        # Extract negState
        negState, msg = self.parsetlv('\xa0', spnego, True)
        status = self.parseenum(negState)
        if status != 1:
            raise SMB_Parse_Exception("Unexpected SPNEGO negotiation status (%d)." % status)

        # Extract supportedMech
        supportedMech, msg = self.parsetlv('\xa1', msg, True)
        if supportedMech!=self.ntlm_oid:
            raise SMB_Parse_Exception("Unexpected SPNEGO mechanism in GSSAPI response.")

        # Extract Challenge, and forget about the rest
        token, msg = self.parsetlv('\xa2', msg, True)
        return self.parseoctstr(token)
 
    def create_smb_header(self, command):
        """Create an SMB header.

        @command        A 1-byte identifier (SMB_COM_*)
        @return         The 32-byte SMB header
        """

        # See 2.2.3.1 in [MS-CIFS]
        hdr =  '\xFFSMB'
        hdr += chr(command)
        hdr += pack('<I', 0)    # Status
        hdr += '\x00'           # Flags
        hdr += pack('<H',       # Flags2
            self.SMB_FLAGS2_EXTENDED_SECURITY   | 
            self.SMB_FLAGS2_NT_STATUS           |
            self.SMB_FLAGS2_UNICODE
            )
        # PID high, SecurityFeatures, Reserved, TID, PID low, UID, MUX ID
        hdr += pack('<H8sHHHHH', 0, '', 0, 0, 0, self.userId, 0)
        return hdr

    def make_negotiate_protocol_req(self):
        """Create an SMB_COM_NEGOTIATE request, that can be sent to the DC.

        The only dialect being negotiated is 'NT LM 0.12'.

        @returns the complete SMB packet, ready to be sent over TCP to the DC.
        """
        self.userId = 0
        hdr = self.create_smb_header(self.SMB_COM_NEGOTIATE)
        params = '\x00'         # Word count
        dialects = '\x02NT LM 0.12\x00'
        data = pack('<H', len(dialects)) + dialects
        return self.addTransport(hdr+params+data)

    def parse_negotiate_protocol_resp(self, response):
        """ Parse a SMB_COM_NEGOTIATE response from the server.

        This function validates the response of a NEGOTIATE request.

        @returns Nothing
        """
        smb_data = self.removeTransport(response)
        hdr = smb_data[:self.SMB_Header_Length]
        msg = smb_data[self.SMB_Header_Length:]
        # WordCount
        idx = 0
        if msg[idx]!='\x11':          # Only accept NT LM 0.12
            raise SMB_Parse_Exception('The server does not support NT LM 0.12')
        # SessionKey
        idx += 16
        self.sessionKey = msg[idx:idx+4]
        # Capabilities
        idx += 4
        capabilities = unpack('<I', msg[idx:idx+4])[0]
        if not(capabilities & self.CAP_EXTENDED_SECURITY):
            raise SMB_Parse_Exception("This server does not support extended security messages.")
        # SystemTime
        idx += 4
        self.systemTime = unpack('<Q', msg[idx:idx+8])[0]
        # ChallengeLength
        idx += 10
        if msg[idx]!='\x00':
            raise SMB_Parse_Exception('No challenge expected, but one found in extended security message.')

    def make_session_setup_req(self, ntlm_token, type1=True):
        """Create an SMB_COM_SESSION_SETUP_ANDX request that can be sent to the DC.

        @ntlm_token     The NTLM message
        @type1          True for Type 1, False for Type 3
        @return         The SMB request
        """
        hdr = self.create_smb_header(self.SMB_COM_SESSION_SETUP_ANDX)

        # Start building SMB_Data, excluding ByteCount
        data = self.make_gssapi_token(ntlm_token, type1)

        # See 2.2.4.53.1 in MS-CIFS and 2.2.4.6.1 in MS-SMB
        params = '\x0C\xFF\x00'             # WordCount, AndXCommand, AndXReserved
        # AndXOffset, MaxBufferSize, MaxMpxCount,VcNumber, SessionKey
        params += pack('<HHHH4s', 0, 1024, 2, 1, self.sessionKey)

        params += pack('<H', len(data))     # SecurityBlobLength
        params += pack('<I',0)              # Reserved
        params += pack('<I',                # Capabilities
              self.CAP_UNICODE  |
              self.CAP_NT_SMBS  |
              self.CAP_STATUS32 |
              self.CAP_EXTENDED_SECURITY)
        
        if (len(data)+len(params))%2==1: data += '\x00'
        data += 'Python\0'.encode('utf-16-le')  # NativeOS
        data += 'Python\0'.encode('utf-16-le')  # NativeLanMan
        return self.addTransport(hdr+params+pack('<H',len(data))+data)

    def parse_session_setup_resp(self, response):
        """Parse the SMB_COM_SESSION_SETUP_ANDX response, as received from the DC.
        
        @response       The SMB response received from the DC
        @return         A tuple where:
                          - the 1st item is a boolean. If False the user
                            is not authenticated
                          - the 2nd item is the NTLM Message2 (1st respone)
                            or is empty (2nd response)
        """

        smb_data = self.removeTransport(response)
        hdr = smb_data[:self.SMB_Header_Length]
        msg = smb_data[self.SMB_Header_Length:]

        status = unpack('<I', hdr[5:9])[0]
        if status==0:
            return (True,'')
        if status!=0xc0000016:
            return (False,'')

        # User ID
        self.userId = unpack('<H',hdr[28:30])[0]
        # WordCount
        idx = 0
        if msg[idx]!='\x04':
            raise SMB_Parse_Exception('Incorrect WordCount')
        # SecurityBlobLength
        idx += 7
        length = unpack('<H', msg[idx:idx+2])[0]
        # Security Blob
        idx += 4
        blob = msg[idx:idx+length]
        return (True,self.extract_gssapi_token(blob))

class NTLM_Proxy:
    """This is a class that handles one single NTLM authentication request like it was
    a domain controller. However, it is just a proxy for the real, remote DC.
    """

    # Raw SMB over IP
    _portdc = 445

    def __init__(self, ipdc, domain, socketFactory=socket, smbFactory=None):
        self.ipdc = ipdc
        self.domain = domain
        self.socketFactory = socketFactory
        self.smbFactory =  smbFactory or (lambda: SMB_Context())
        self.socket = False
        self.bufferin = ''

    def _openDCConnection(self):
        """Open a connection to the DC, and reset any existing one."""

        self.close()
        self.socket = self.socketFactory.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        self.socket.connect((self.ipdc, self._portdc))

    def _readsocket(self, length):
        """Read exactly @length bytes from the socket"""
        data = self.bufferin
        while len(data)<length:
            data += self.socket.recv(1024)
        data, self.bufferin = data[:length], data[length:]
        return data

    def _transaction(self, msg):
        self.socket.send(msg)
        data = self._readsocket(self.smb.Transport_Header_Length)
        data += self._readsocket(self.smb.getTransportLength(data))
        return data

    def close(self):
        if self.socket:
            self.socket.close()
            self.socket = False
        self.bufferin = ''

    def negotiate(self, ntlm_negotiate):
        """Accept a Negotiate NTLM message (Type 1), and return a Challenge message (Type 2)."""
       
        # First transaction: open the connection
        self._openDCConnection()
        self.smb = self.smbFactory()
        msg = self.smb.make_negotiate_protocol_req()
        msg = self._transaction(msg)
        self.smb.parse_negotiate_protocol_resp(msg)

        # Second transaction: get the challenge
        msg = self.smb.make_session_setup_req(ntlm_negotiate, True)
        msg = self._transaction(msg)
        result, challenge = self.smb.parse_session_setup_resp(msg)
        if not result:
            return False
        return challenge

    def authenticate(self, ntlm_authenticate):
        """Accept an Authenticate NTLM message (Type 3), and return True if the user and credentials are correct."""

        msg = self.smb.make_session_setup_req(ntlm_authenticate, False)
        msg = self._transaction(msg)
        self.close()
        return self.smb.parse_session_setup_resp(msg)[0]

    
