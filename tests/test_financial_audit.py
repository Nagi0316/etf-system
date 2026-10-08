"""Numerical and transaction regressions found during the financial audit."""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
from pydantic import ValidationError

import authentication
import database
from fastapi import HTTPException
from starlette.requests import Request
from memory_cache import cache
from request_models import BacktestIn, BacktestCompareIn, TransactionIn
from routes.portfolio_routes import _insert_transaction
from services.backtest_engine import run_accumulate, _money_weighted_return, _compute_risk_metrics
from services.exchange_rate_service import get_usd_twd


def run(hist, **kwargs):
    config = dict(initial_amount=0, monthly_amount=1000, price_mode='open',
                  enable_drip=False, enable_dip=False, dip_threshold_20d=10,
                  dip_threshold_60d=15, dip_extra_pct=50)
    config.update(kwargs)
    return run_accumulate(hist, **config)


def prices(dates, closes):
    return pd.DataFrame({c: closes for c in ('Open', 'Close', 'High', 'Low')},
                        index=pd.to_datetime(dates))


class FinancialAuditTest(unittest.TestCase):
    def test_xirr_uses_late_contribution_date(self):
        txs = [{'date': '2024-01-01', 'type': '期初單筆', 'amount': 1000},
               {'date': '2024-12-31', 'type': '定期定額', 'amount': 1000}]
        # First 1000 earned 10%; final-day 1000 earned nothing.
        self.assertAlmostEqual(_money_weighted_return(txs, pd.Timestamp('2024-12-31'), 2100),
                               (1.1 ** (365.25 / 365) - 1) * 100, places=5)

    def test_same_day_is_not_annualized(self):
        r = run(prices(['2024-01-02'], [100]))
        self.assertIsNone(r['annual_return'])

    def test_dividend_entitlement_excludes_ex_date_purchase(self):
        hist = prices(['2024-01-02', '2024-02-01', '2024-02-02'], [100, 100, 100])
        divs = pd.Series([10], index=pd.to_datetime(['2024-02-01']))
        r = run(hist, dividend_series=divs)
        self.assertAlmostEqual(r['cash_balance'], 98)  # January: (1000-20)/100 shares
        self.assertAlmostEqual(r['final_value'], 2058)

    def test_drip_uses_dividend_day_price(self):
        hist = prices(['2024-01-02', '2024-01-15', '2024-01-31'], [100, 50, 50])
        divs = pd.Series([10], index=pd.to_datetime(['2024-01-15']))
        r = run(hist, enable_drip=True, dividend_series=divs)
        tx = next(t for t in r['transactions'] if t['type'] == 'DRIP配息再投入')
        self.assertEqual(tx['date'], '2024-01-15')
        self.assertEqual(tx['price'], 50)
        self.assertAlmostEqual(tx['shares_delta'], 1.56)

    def test_dividend_below_fee_is_preserved_as_cash(self):
        hist = prices(['2024-01-02', '2024-01-03'], [100, 100])
        r = run(hist, enable_drip=True,
                dividend_series=pd.Series([1], index=pd.to_datetime(['2024-01-03'])))
        self.assertEqual(r['cash_balance'], 9.8)
        self.assertFalse(any(t['type'] == 'DRIP配息再投入' for t in r['transactions']))

    def test_hypothetical_low_purchase_date_matches_price(self):
        r = run(prices(['2024-01-02', '2024-01-31'], [100, 50]), price_mode='low')
        self.assertEqual(r['transactions'][0]['date'], '2024-01-31')
        self.assertEqual(r['transactions'][0]['price'], 50)

    def test_drawdown_includes_first_day_and_calmar_keeps_negative_sign(self):
        hist = prices(pd.bdate_range('2024-01-01', periods=30), [100] + [50] * 29)
        r = _compute_risk_metrics(hist, 999)
        self.assertEqual(r['max_drawdown'], -50)
        self.assertLess(r['calmar_ratio'], 0)

    def test_sortino_uses_downside_deviation(self):
        closes = [100 * 0.99 ** i for i in range(30)]
        r = _compute_risk_metrics(prices(pd.bdate_range('2024-01-01', periods=30), closes), -50)
        self.assertAlmostEqual(r['sortino_ratio'], -np.sqrt(252), places=2)

    def test_invalid_backtest_inputs_are_rejected_on_both_endpoints(self):
        for model in (BacktestIn, BacktestCompareIn):
            for args in ({'monthly_amount': -1}, {'initial_amount': float('nan')},
                         {'start_date': '2024-12-31', 'end_date': '2024-01-01'},
                         {'price_mode': 'garbage'}, {'start_date': 'not-a-date'}):
                with self.subTest(model=model, args=args), self.assertRaises(ValidationError):
                    model(**args)
        with self.assertRaises(ValidationError):
            TransactionIn(ticker='0050', transaction_type='buy', shares=float('inf'),
                          price=10, transaction_date='2024-01-01')

    def test_unknown_fx_is_not_fabricated(self):
        with patch('services.exchange_rate_service.cache.get', return_value=None), \
             patch('services.exchange_rate_service._fetch_usd_twd', return_value=0):
            with self.assertRaises(RuntimeError):
                get_usd_twd()


class AuthenticationAuditTest(unittest.TestCase):
    def tearDown(self):
        cache.delete("jti:ok:audit-missing")
        cache.delete("jti:revoked:audit-missing")

    def test_missing_session_record_is_denied(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        @contextmanager
        def fake_db():
            yield MagicMock(), cursor
        request = Request({'type': 'http', 'headers': [], 'method': 'GET', 'path': '/'})
        with patch.object(authentication, 'decode_token', return_value={'sub': '1', 'jti': 'audit-missing'}), \
             patch.object(authentication, '_get_token_from_request', return_value='token'), \
             patch.object(authentication, 'get_db', fake_db):
            with self.assertRaises(HTTPException) as exc:
                authentication.get_current_user(request, None)
        self.assertEqual(exc.exception.status_code, 401)

    def test_missing_jti_is_denied(self):
        request = Request({'type': 'http', 'headers': [], 'method': 'GET', 'path': '/'})
        with patch.object(authentication, 'decode_token', return_value={'sub': '1'}), \
             patch.object(authentication, '_get_token_from_request', return_value='token'):
            with self.assertRaises(HTTPException):
                authentication.get_current_user(request, None)


class PortfolioChronologyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(database, 'USE_MYSQL', False),
                        patch.object(database, 'SQLITE_PATH', str(Path(self.tmp.name) / 'audit.db'))]
        for p in self.patches:
            p.start()
        database.init_db()
        with database.get_db() as (conn, cur):
            cur.execute("INSERT INTO users (id,username,email) VALUES (1,'test','test@example.com')")
            conn.commit()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def trade(self, kind, shares, day, key):
        _insert_transaction(1, dict(ticker='0050', transaction_type=kind, shares=shares,
                                   price=100, transaction_date=day), key)

    def test_backdated_sell_cannot_use_future_purchase(self):
        self.trade('buy', 10, '2024-02-01', 'buy')
        with self.assertRaises(ValueError):
            self.trade('sell', 5, '2024-01-01', 'sell')
        with database.get_db() as (_, cur):
            cur.execute('SELECT COUNT(*) AS n FROM user_transactions')
            self.assertEqual(cur.fetchone()['n'], 1)
            cur.execute('SELECT shares FROM user_portfolio')
            self.assertEqual(cur.fetchone()['shares'], 10)

    def test_same_idempotency_key_is_rejected_without_cache(self):
        self.trade('buy', 10, '2024-01-01', 'same-key')
        with self.assertRaises(ValueError):
            self.trade('buy', 10, '2024-01-01', 'same-key')
        with database.get_db() as (_, cur):
            cur.execute('SELECT shares FROM user_portfolio')
            self.assertEqual(cur.fetchone()['shares'], 10)

    def test_historical_sell_uses_historical_inventory_not_today_balance(self):
        self.trade('buy', 10, '2024-01-01', 'buy1')
        self.trade('sell', 5, '2024-03-01', 'sell1')
        self.trade('sell', 5, '2024-02-01', 'sell2')
        with database.get_db() as (_, cur):
            cur.execute('SELECT shares FROM user_portfolio')
            self.assertEqual(cur.fetchone()['shares'], 0)

class BacktestEndpointAuditTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from main import app
        self.client = TestClient(app)
        self.hist = prices(['2024-01-02', '2024-02-01', '2024-02-29'], [100, 105, 110])

    def test_endpoints_include_cash_dividends_even_without_drip(self):
        from routes import backtest_routes as br
        dividends = pd.Series([2], index=pd.to_datetime(['2024-02-01']))
        with patch.object(br, '_get_market', return_value='TW'), \
             patch.object(br, '_download_hist', return_value=self.hist), \
             patch.object(br, '_get_dividends', return_value=dividends):
            payload = {'ticker': '0050', 'start_date': '2024-01-01', 'end_date': '2024-02-29',
                       'monthly_amount': 1000, 'initial_amount': 1000,
                       'enable_drip': False, 'enable_dip': True, 'benchmark_ticker': '0056'}
            r = self.client.post('/api/backtest', json=payload)
            self.assertEqual(r.status_code, 200, r.text)
            d = r.json()['data']
            self.assertGreater(d['cash_balance'], 0)
            self.assertEqual(d['annual_return_method'], 'xirr')
            self.assertEqual(d['total_invested'], d['benchmark']['total_invested'])
            self.assertIn('不保證', d['strategy_note']['description'])
            r = self.client.post('/api/backtest/compare', json=payload)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertGreater(r.json()['data']['open']['cash_balance'], 0)

    def test_suspected_unadjusted_split_does_not_produce_fake_loss(self):
        from routes import backtest_routes as br
        with patch.object(br, '_get_market', return_value='TW'), \
             patch.object(br, '_download_hist', return_value=prices(
                 ['2024-01-02', '2024-01-03'], [100, 25])):
            r = self.client.post('/api/backtest', json={'ticker': '0050'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('分割', r.json()['message'])

    def test_zero_duration_and_small_budget_are_handled(self):
        from routes import backtest_routes as br
        with patch.object(br, '_get_market', return_value='TW'), \
             patch.object(br, '_download_hist', return_value=prices(['2024-01-02'], [100])), \
             patch.object(br, '_get_dividends', return_value=pd.Series(dtype=float)):
            r = self.client.post('/api/backtest', json={'enable_drip': True})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIsNone(r.json()['data']['strategy_note']['boost'])
            r = self.client.post('/api/backtest', json={'monthly_amount': 1})
            self.assertEqual(r.status_code, 400)

    def test_bond_etf_tax_exemption_has_a_date_boundary(self):
        from routes.backtest_routes import _exit_tax_rate
        self.assertEqual(_exit_tax_rate('00679B', 'TW', '2026-12-31'), 0)
        self.assertEqual(_exit_tax_rate('00679B', 'TW', '2027-01-01'), 0.001)
        self.assertEqual(_exit_tax_rate('0050', 'TW', '2024-01-01'), 0.001)
        self.assertEqual(_exit_tax_rate('BND', 'US', '2024-01-01'), 0)

    def test_missing_real_dividends_are_not_synthesized_from_current_yield(self):
        from routes import backtest_routes as br
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.return_value = {'dividend_yield': 8, 'payout_freq': '月配'}
        @contextmanager
        def fake_db():
            yield MagicMock(), cur
        with patch.object(database, 'get_db', fake_db), \
             patch.object(br, '_download_hist_from_db', return_value=self.hist):
            divs = br._get_dividends_from_db('0050', '2024-01-01', '2024-02-29')
        self.assertTrue(divs.empty)


class TradingCostsAuditTest(unittest.TestCase):
    def test_market_defaults_and_explicit_zero_are_distinct(self):
        from request_models import BacktestIn
        from routes.backtest_routes import _trading_costs
        default = BacktestIn()
        self.assertEqual(_trading_costs('US', default), {'commission_rate': 0.0, 'min_commission': 0.0})
        self.assertEqual(_trading_costs('TW', default)['min_commission'], 20)
        custom = BacktestIn(commission_rate=0, min_commission=0)
        self.assertEqual(_trading_costs('TW', custom)['min_commission'], 0)

    def test_zero_commission_small_budget_can_buy(self):
        from services.backtest_engine import _calc_shares
        self.assertEqual(_calc_shares(10, 1, 0, 0), (0.1, 0))
        self.assertEqual(_calc_shares(10, 1), (0, 0))


class OAuthEmailAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_email_cannot_link_a_member(self):
        from unittest.mock import AsyncMock
        from routes import authentication_routes as ar
        with patch.object(ar, 'exchange_google_code', new=AsyncMock(return_value=(
            {'sub': 'some-id', 'email': 'victim@example.com', 'email_verified': False}, '/'))), \
             patch.object(ar, 'get_db') as db:
            response = await ar.google_callback(code='code', state='state')
        self.assertIn('unverified_email', response.headers['location'])
        db.assert_not_called()


if __name__ == '__main__':
    unittest.main()
