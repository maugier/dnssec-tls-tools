import calendar
import hashlib
import struct
import subprocess
import sys
import time

kDSDigestTypeSHA1 = 1
kDSDigestTypeSHA256 = 2
kRootKeyTag = 19036

kDNSTypeCNAME = 5
kDNSTypeTXT = 16
kDNSTypeDS = 43

class DigError(Exception):
  def __init__(self, s):
    self.msg = s
  def __str__(self):
    return self.msg

class DNSError(Exception):
  def __init__(self, s):
    self.msg = s
  def __str__(self):
    return self.msg

def parseDate(s):
  t = time.strptime(s, '%Y%m%d%H%M%S')
  return int(calendar.timegm(t))

class RRSIG(object):
  def __init__(self, t):
    parts = t.split(None, 8)
    if len(parts) != 9:
      raise DigError('Bad RRSIG: %s' % t)
    self.rrtype = parts[0]
    self.algorithm = int(parts[1])
    self.labels = int(parts[2])
    self.ttl = int(parts[3])
    self.expires = parseDate(parts[4])
    self.begins = parseDate(parts[5])
    self.keyTag = int(parts[6])
    self.signer = parts[7]
    self.signature = parts[8].replace(' ', '').decode('base64')

  def Serialise(self, out):
    out.U8(self.algorithm)
    out.U8(self.labels)
    out.U32(self.ttl)
    out.U32(self.expires)
    out.U32(self.begins)
    out.U16(self.keyTag)
    out.append(self.signature)

class RR(object):
  def __init__(self, domain, rrtype):
    self.domain = domain
    self.rrtype = rrtype
    self.rrsigs = []

  def fetch(self):
    dig = subprocess.Popen(['dig', '@127.0.0.1', '+dnssec', '-t', self.rrtype, self.domain], stdout=subprocess.PIPE)
    (stdout, _) = dig.communicate()
    lines = [x for x in stdout.split('\n') if len(x) > 0 and x[0] != ';']
    self.digOutput = lines
    ret = []
    for l in lines:
      parts = l.split(None, 4)
      if parts[0] == self.domain and parts[2] == 'IN':
        if parts[3] == self.rrtype:
          ret.append(parts[4])
        elif parts[3] == 'RRSIG':
          rrsig = RRSIG(parts[4])
          if rrsig.rrtype == self.rrtype:
            self.rrsigs.append(rrsig)
    return ret

class CNAME(RR):
  def __init__(self, domain):
    super(CNAME, self).__init__(domain, 'CNAME')
    cnames = self.fetch()
    if len(cnames) == 0:
      self.cname = None
    else:
      self.cname = cnames[0]

def parseQuotedString(t):
  inString = False
  quoting = False
  r = ''
  for c in t:
    if not inString:
      if c == '"':
        inString = True
        continue
      elif c == ' ' or c == '\t':
        continue
      else:
        return None
    if quoting:
      r += c
      continue
    if c == '\\':
      quoting = True
      continue
    if c == '"':
      inString = False
      continue
    r += c
  return r

def serialiseTXT(t):
  out = Output()
  while len(t) > 0:
    piece = t[:255]
    out.U8(len(piece))
    out.append(piece)
    t = t[len(piece):]
  return out.Bytes()

class TXT(RR):
  def __init__(self, domain):
    super(TXT, self).__init__(domain, 'TXT')
    txts = self.fetch()
    self.txts = []
    for t in txts:
      if t.startswith('"'):
        tt = parseQuotedString(t)
        if tt is None:
          raise DigError('Invalid quoted string: %s' % t)
        self.txts.append(tt)
      else:
        self.txts.append(t)
    self.txts.sort()

  def Valid(self):
    for t in self.txts:
      if 'v=tls1' in t:
        return True
    return False

def toDNSName(name):
  labels = name.split('.')
  out = Output()
  for l in labels:
    if len(l) == 0:
      continue
    out.U8(len(l))
    out.append(l)
  out.U8(0)
  return out.Bytes()

def serialiseKey(key):
  if 'serialised' in key:
    return key['serialised']
  out = Output()
  out.U16(key['flags'])
  out.U8(key['protocol'])
  out.U8(key['algorithm'])
  out.append(key['key'])
  return out.Bytes()

def digestKey(proto, key, name):
  data = toDNSName(name) + serialiseKey(key)
  if proto == kDSDigestTypeSHA1:
    return hashlib.sha1(data).digest()
  else:
    return hashlib.sha256(data).digest()

def keyTag(key):
  ac = 0
  for (i, b) in enumerate(key):
    if i & 1 == 1:
      ac += ord(b)
    else:
      ac += ord(b) << 8
  ac += (ac >> 16) & 0xffff
  return ac & 0xffff

class DNSKEY(RR):
  def __init__(self, domain):
    super(DNSKEY, self).__init__(domain, 'DNSKEY')
    keys = self.fetch()
    self.keys = []
    for k in keys:
      parts = k.split(None, 3)
      key = {}
      key['flags'] = int(parts[0])
      key['protocol'] = int(parts[1])
      key['algorithm'] = int(parts[2])
      key['key'] = parts[3].replace(' ', '').decode('base64')
      key['serialised'] = serialiseKey(key)
      key['tag'] = keyTag(serialiseKey(key))
      self.keys.append(key)
    self.keys.sort(key = lambda x: x['serialised'])

def serialiseDS(ds):
  out = Output()
  out.U16(ds['keytag'])
  out.U8(ds['algorithm'])
  out.U8(ds['digestType'])
  out.append(ds['digest'])
  return out.Bytes()

class DS(RR):
  def __init__(self, domain):
    super(DS, self).__init__(domain, 'DS')
    dses = self.fetch()
    self.dses = []
    for d in dses:
      parts = d.split(None, 3)
      ds = {}
      ds['keytag'] = int(parts[0])
      ds['algorithm'] = int(parts[1])
      ds['digestType'] = int(parts[2])
      ds['digest'] = parts[3].replace(' ', '').decode('hex')
      ds['serialised'] = serialiseDS(ds)
      self.dses.append(ds)
    self.dses.sort(key = lambda x: x['serialised'])

class SOA(RR):
  def __init__(self, domain):
    super(SOA, self).__init__(domain, 'SOA')
    self.fetch()
    for l in self.digOutput:
      parts = l.split(None, 4)
      if parts[2] == 'IN' and parts[3] == 'SOA':
        self.soa = parts[0]
        break

class Zone(object):
  pass

def removeLeadingLabel(t):
  if len(t) == 1:
    return t
  parts = t.split('.', 1)
  if len(parts) > 1:
    if len(parts[1]) == 0:
      return '.'
    return parts[1]
  return parts[0]

class Output(object):
  def __init__(self):
    self.a = []

  def U32(self, v):
    self.a.append(struct.pack('>I', v))

  def U16(self, v):
    self.a.append(struct.pack('>H', v))

  def U8(self, v):
    self.a.append(struct.pack('>B', v))

  def append(self, v):
    self.a.append(v)

  def Bytes(self):
    return ''.join(self.a)

def buildChain(target):
  cname = CNAME(target)
  terminal = None
  if cname.cname is not None:
    print '%s is a CNAME for %s' % (target, cname.cname)
    terminal = cname
  else:
    terminal = TXT(target)
    if not terminal.Valid():
      print 'No good TXT records at %s' % target

  zoneNames = []
  t = target
  print 'Zone listing for', target
  while True:
    z = SOA(t).soa
    zoneNames.append(z)
    print ' ', z
    if t == '.':
      break
    t = removeLeadingLabel(z)

  zones = []
  zoneNames.reverse()
  for (i, z) in enumerate(zoneNames):
    zone = Zone()
    zone.name = z
    zones.append(zone)

  for (i, z) in enumerate(zones):
    z.prevZone = None
    z.nextZone = None
    if i > 0:
      z.prevZone = zones[i-1]
    if i + 1 < len(zones):
      z.nextZone = zones[i+1]

  for z in zones:
    z.directKey = False
    z.alreadyInZone = False
    z.dnskey = DNSKEY(z.name)
    if z.nextZone is not None:
      z.ds = DS(z.nextZone.name)
    else:
      z.terminal = terminal

  for z in zones:
    exitRecord = None
    if z.nextZone is not None:
      exitRecord = z.ds
    else:
      exitRecord = z.terminal

    # now we find the keys which sign the exit records
    exitSigners = set()
    for rrsig in exitRecord.rrsigs:
      for (i, key) in enumerate(z.dnskey.keys):
        if rrsig.keyTag == key['tag']:
          exitSigners.add(i)

    if z.prevZone is None:
      # root zone
      for (i, key) in enumerate(z.dnskey.keys):
        if key['tag'] == kRootKeyTag:
          z.entryKey = i
          break
      else:
        raise DNSError('Failed to find root entry key')
    else:
      # look through the previous zone's DS records for possible entry keys.
      entryKeys = set()
      for ds in z.prevZone.ds.dses:
        if ds['digestType'] in [kDSDigestTypeSHA1, kDSDigestTypeSHA256]:
          for (i, key) in enumerate(z.dnskey.keys):
            if digestKey(ds['digestType'], key, z.name) == ds['digest']:
              ds['elide'] = True
              entryKeys.add(i)

      # If we can enter on a key which also signs the exit record then we can
      # avoid including a signature over the DNSKEYs.
      preferredKeys = exitSigners.intersection(entryKeys)
      if len(preferredKeys) > 0:
        z.entryKey = list(preferredKeys)[0]
        z.directKey = True
      else:
        z.entryKey = list(entryKeys)[0]

    if not z.directKey:
     # Need to select the DNSKEY signature
     for rrsig in z.dnskey.rrsigs:
       if rrsig.keyTag == z.dnskey.keys[z.entryKey]['tag']:
         z.dnsKeySig = rrsig
         break
     else:
       raise DNSError('Failed to find any signatures for the entry key: %s' % z.name)

    # Need to select the signature over the exit record
    if not z.directKey:
      # any key in the keyset will do
      for keyIndex in exitSigners:
        tag = z.dnskey.keys[keyIndex]['tag']
        for rrsig in exitRecord.rrsigs:
          if rrsig.keyTag == tag:
            z.exitRecordSig = rrsig
            break
        else:
          assert(False)
        break
      else:
        raise DNSError('The exit record is not signed by any trusted key: %s' % z.name)
    else:
      # the exit record must be signed by the entry key
     for rrsig in exitRecord.rrsigs:
       if rrsig.keyTag == z.dnskey.keys[z.entryKey]['tag']:
         z.exitRecordSig = rrsig
         break
       else:
         assert(False)

  return zones

def serialiseZones(out, zones, target):
  for z in zones:
    if not z.alreadyInZone:
      out.U8(z.entryKey)
      if not z.directKey:
        o = Output()
        z.dnsKeySig.Serialise(o)
        serialised = o.Bytes()
        out.U16(len(serialised))
        out.append(serialised)
      else:
        print '  using direct keying for', z.name
        out.U16(0)

      if not z.directKey:
        out.U8(len(z.dnskey.keys))
        for key in z.dnskey.keys:
          if z.prevZone is None and key['tag'] == kRootKeyTag:
            serialised = ''
          else:
            serialised = serialiseKey(key)
          out.U16(len(serialised))
          out.append(serialised)
      else:
        out.U8(1)
        key = z.dnskey.keys[z.entryKey]
        serialised = serialiseKey(key)
        out.U16(len(serialised))
        out.append(serialised)

    nextName = None
    if z.nextZone is not None:
      nextName = z.nextZone.name
    else:
      nextName = target
    nextName = toDNSName(nextName)
    out.append(nextName)

    if z.nextZone is not None:
      out.U16(kDNSTypeDS)
    elif type(z.terminal) == TXT:
      out.U16(kDNSTypeTXT)
    elif type(z.terminal) == CNAME:
      out.U16(kDNSTypeCNAME)
    else:
      assert(False)

    o = Output()
    z.exitRecordSig.Serialise(o)
    serialised = o.Bytes()
    out.U16(len(serialised))
    out.append(serialised)

    if z.nextZone is not None:
      out.U8(len(z.ds.dses))
      for ds in z.ds.dses:
        out.U8(ds['digestType'])
        if 'elide' in ds and ds['elide']:
          serialised = ''
        else:
          serialised = serialiseDS(ds)
        out.U16(len(serialised))
        out.append(serialised)
    else:
      if type(z.terminal) == TXT:
        txts = z.terminal.txts
        out.U8(len(txts))
        for t in txts:
          serialised = serialiseTXT(t)
          out.U16(len(serialised))
          out.append(serialised)
      else:
        out.append(toDNSName(z.terminal.cname))

    print "After %s: %d bytes" % (z.name, len(out.Bytes()))

def spliceZones(new, old):
  i = 0
  while i < len(new) and i < len(old) and new[i].name == old[i].name:
    i += 1
  new = new[i-1:]
  new[0].alreadyInZone = True
  return new

def main():
  if len(sys.argv) != 3:
    print 'Usage: <target DNS name> <output filename>'
    return
  target = sys.argv[1]
  if not target.endswith('.'):
    target = '%s.' % target

  out = Output()
  out.U16(kRootKeyTag)

  previousZones = None
  while True:
    zones = buildChain(target)
    if previousZones is not None:
      zones = spliceZones(zones, previousZones)
    serialiseZones(out, zones, target)
    if type(zones[-1].terminal) != CNAME:
      break
    target = zones[-1].terminal.cname
    print 'Building new chain targetting %s' % target
    previousZones = zones

  file(sys.argv[2], 'w+').write(out.Bytes())

if __name__ == '__main__':
  main()
