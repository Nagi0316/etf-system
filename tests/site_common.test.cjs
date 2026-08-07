const assert = require('node:assert/strict');
const test = require('node:test');

const classList = {
  add() {},
  contains() { return false; },
  toggle() { return false; },
};

global.localStorage = {
  getItem() { return null; },
  removeItem() {},
  setItem() {},
};
global.window = {
  location: { pathname: '/', search: '' },
  matchMedia() { return { matches: false }; },
};
global.document = {
  addEventListener() {},
  cookie: '',
  documentElement: { classList },
  getElementById() { return null; },
  querySelectorAll() { return []; },
};

const {
  fmt,
  fmtMoney,
  fmtPct,
  getDividendDisplay,
} = require('../static/js/site_common.js');

test('number formatters reject invalid API values', () => {
  assert.equal(fmt(null), '—');
  assert.equal(fmt(''), '—');
  assert.equal(fmt('invalid'), '—');
  assert.equal(fmtPct(Infinity), '—');
  assert.equal(fmtMoney(Number.NaN), '—');
});

test('number formatters preserve valid numeric strings', () => {
  assert.equal(fmt('12.345', 2), '12.35');
  assert.equal(fmtPct('-1.2'), '-1.20%');
  assert.equal(fmtMoney('1234', 'TWD'), 'NT$1,234');
});

test('estimated dividend yield keeps internal status without UI marker', () => {
  const display = getDividendDisplay({
    dividend_yield: 3.456,
    dividend_status: 'estimated',
    payout_freq: '季配',
  });

  assert.equal(display.text, '3.46%');
  assert.equal(display.status, 'estimated');
  assert.equal(display.frequency, '季配');
  assert.equal(display.hasYield, true);
});

test('missing and non-distributing dividends remain distinct', () => {
  assert.equal(getDividendDisplay({}).text, '待同步');
  assert.equal(
    getDividendDisplay({ dividend_status: 'not_applicable' }).text,
    '不配息',
  );
});
