const { spawn } = require('node:child_process');
const { writeFileSync } = require('node:fs');

const child = spawn(
  process.execPath,
  ['-e', 'setInterval(() => {}, 1000)'],
  { stdio: 'ignore' },
);
writeFileSync('node-child.pid', String(child.pid), 'ascii');
// The parent-ready marker is the test's synchronization point. Write it last
// so the test never observes startup before the child PID is available.
writeFileSync('node-parent.started', 'started', 'utf8');
setInterval(() => {}, 1000);
