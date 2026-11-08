// Local cross-repository contract fixture: two real Redis ledgers, fake app DB.
const root = process.argv[2];
const {createClient} = require(root + '/node_modules/redis');
const express = require(root + '/node_modules/express');
const {createIngress} = require(root + '/src/durable-ingress/service');
const {once} = require('node:events');
const readline = require('node:readline');
(async () => {
  const clients = process.argv.slice(3).map(url => createClient({url}));
  await Promise.all(clients.map(client => client.connect()));
  const rows = new Map();
  const backend = express(); backend.use(express.json());
  backend.get('/readyz', (req, res) => res.json({status: 'ok'}));
  backend.get('/api/info', (req, res) => res.json({environment: 'aws',
    database: {name: 'migration_demo'}, version: 'fixture-v1', writes_paused: false}));
  backend.post('/api/requests', (req, res) => {
    const old = rows.get(req.body.request_id);
    if (old && JSON.stringify(old) !== JSON.stringify(req.body)) return res.sendStatus(409);
    rows.set(req.body.request_id, req.body);
    res.json({status: 'committed', request: req.body});
  });
  backend.get('/api/requests/:id', (req, res) => {
    const request = rows.get(req.params.id);
    return request ? res.json({status: 'committed', request}) : res.sendStatus(404);
  });
  const server = backend.listen(0, '127.0.0.1'); await once(server, 'listening');
  const appUrl = `http://127.0.0.1:${server.address().port}`;
  async function start(redis, options) {
    const app = await createIngress({redis, targets: {onprem: {url: appUrl}, aws_eks: {url: appUrl}},
      controlToken: 'contract-control', ...options});
    const api = app.api.listen(0, '127.0.0.1'), control = app.control.listen(0, '127.0.0.1');
    await Promise.all([once(api, 'listening'), once(control, 'listening')]);
    return {app, api, control, url: `http://127.0.0.1:${api.address().port}`,
      controlUrl: `http://127.0.0.1:${control.address().port}`};
  }
  const destination = await start(clients[1], {standby: true});
  const source = await start(clients[0], {forwardTargets: {aws_receipt: {url: destination.url}}});
  process.stdout.write(JSON.stringify({source: source.controlUrl, destination: destination.controlUrl,
    receipt: destination.url, app: appUrl, sourceApi: source.url}) + '\n');
  let sourceStopped = false;
  async function stop(item) {
    await item.app.close();
    await Promise.all([new Promise(r => item.api.close(r)), new Promise(r => item.control.close(r))]);
  }
  readline.createInterface({input: process.stdin}).on('line', async line => {
    if (line === 'stop-source') { await stop(source); sourceStopped = true; process.stdout.write('stopped\n'); }
  });
  process.on('SIGTERM', async () => {
    if (!sourceStopped) await stop(source);
    await stop(destination); server.close();
    await Promise.all(clients.map(client => client.quit())); process.exit(0);
  });
})().catch(error => { console.error(error); process.exit(1); });
