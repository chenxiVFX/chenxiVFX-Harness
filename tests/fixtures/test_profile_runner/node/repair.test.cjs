const test = require('node:test');
const assert = require('node:assert/strict');

test('Node 合成项目需要一次精确修补', () => {
  assert.equal(2 + 2, 5);
  console.log('Node 修补后复测通过');
});
