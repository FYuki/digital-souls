'use strict';

// promptfoo の telemetry opt-out 自体も送信を試みるため、Node の通信は全面拒否する。
// 実モデルは別の Python provider だけが明示 loopback profile で接続する。
const { syncBuiltinESMExports } = require('node:module');
function deny() { throw new Error('Semantic evaluation: Node network access is disabled'); }
globalThis.fetch = async () => deny();
for (const protocol of ['node:http', 'node:https']) {
  const api = require(protocol);
  api.request = deny;
  api.get = deny;
}
require('node:http2').connect = deny;
const net = require('node:net');
net.connect = deny;
net.createConnection = deny;
net.Socket.prototype.connect = deny;
require('node:tls').connect = deny;
require('node:dgram').createSocket = deny;
const dns = require('node:dns');
for (const name of Object.keys(dns)) {
  if (name === 'lookup' || name === 'lookupService' || name.startsWith('resolve') || name === 'reverse') {
    dns[name] = deny;
  }
}
for (const name of Object.keys(dns.promises)) {
  if (name === 'lookup' || name === 'lookupService' || name.startsWith('resolve') || name === 'reverse') {
    dns.promises[name] = async () => deny();
  }
}
for (const Resolver of [dns.Resolver, dns.promises.Resolver]) {
  for (const name of Object.getOwnPropertyNames(Resolver.prototype)) {
    if (name.startsWith('resolve') || name === 'reverse') Resolver.prototype[name] = deny;
  }
}
syncBuiltinESMExports();
