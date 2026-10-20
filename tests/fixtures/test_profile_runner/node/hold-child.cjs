const { spawn } = require('node:child_process');
const { writeFileSync } = require('node:fs');

const child = spawn(
  process.execPath,
  ['-e', 'setInterval(() => {}, 1000)'],
  { stdio: 'ignore' },
);
writeFileSync('node-parent.started', 'started', 'utf8');
writeFileSync('node-child.pid', String(child.pid), 'ascii');
setInterval(() => {}, 1000);
