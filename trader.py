import yaml, time, json, math, traceback, os
from datetime import datetime, timedelta
from utils.binance_client import create_client
import pandas as pd
import ta

class TradeManager:
    def __init__(self, cfg_path="config.yaml"):
        with open(cfg_path, "r") as f:
            self.cfg = yaml.safe_load(f)
        self.client = create_client()
        self.pairs = self.cfg.get("pairs", ["BTCUSDT"])
        self.q = self.cfg['order']['qty']
        self.leverage = self.cfg['order']['leverage']
        self.tp_pct = self.cfg['order']['take_profit_pct']
        self.sl_pct = self.cfg['order']['stop_loss_pct']
        self.trailing_enable = self.cfg['order'].get('trailing_enable', True)
        self.trail_trigger = self.cfg['order'].get('trailing_trigger_pct', 0.5)
        self.trail_delta = self.cfg['order'].get('trailing_delta_pct', 0.3)
        self.order_timeout = self.cfg['timeouts'].get('order_timeout_sec', 900)
        self.time_filter_cfg = self.cfg.get('time_filter', {})
        self.trades_log = self.cfg['logging'].get('trades_log', './logs/trades.json')

        self.positions = {}
        self.ensure_logs()

        for p in self.pairs:
            try:
                self.client.futures_change_leverage(symbol=p, leverage=self.leverage)
            except Exception:
                pass

    def ensure_logs(self):
        d = os.path.dirname(self.trades_log)
        if d and not os.path.exists(d):
            os.makedirs(d)
        if not os.path.exists(self.trades_log):
            with open(self.trades_log, "w") as f:
                json.dump({"trades": [], "positions": {}}, f)

    def log_trade_local(self, record):
        try:
            with open(self.trades_log, "r+") as f:
                data = json.load(f)
                data.setdefault("trades", []).append(record)
                data["positions"] = self.positions
                f.seek(0)
                json.dump(data, f, default=str, indent=2)
                f.truncate()
        except Exception as e:
            print("log error", e)

    def in_trading_time(self):
        tf = self.time_filter_cfg
        if not tf.get("enabled", False):
            return True
        tz_off = int(tf.get("tz_offset_hours", 0))
        start = int(tf.get("start_hour", 0))
        end = int(tf.get("end_hour", 23))
        now_utc = datetime.utcnow()
        local_hour = (now_utc.hour + tz_off) % 24
        return start <= local_hour <= end

    def compute_tp_sl(self, entry_price, side):
        if side == "BUY":
            tp = entry_price * (1 + self.tp_pct/100)
            sl = entry_price * (1 - self.sl_pct/100)
        else:
            tp = entry_price * (1 - self.tp_pct/100)
            sl = entry_price * (1 + self.sl_pct/100)
        return tp, sl

    def compute_qty_by_fixed(self):
        return float(self.q)

    def open_market_position(self, symbol, side):
        qty = self.compute_qty_by_fixed()
        try:
            res = self.client.futures_create_order(symbol=symbol, side=side, type='MARKET', quantity=qty)
            fills = res.get("fills", [])
            price = None
            if fills:
                price = float(fills[0].get("price"))
            else:
                ticker = self.client.futures_symbol_ticker(symbol=symbol)
                price = float(ticker['price'])
            tp, sl = self.compute_tp_sl(price, side)
            order_id = res.get("orderId")
            try:
                if side == "BUY":
                    self.client.futures_create_order(symbol=symbol, side='SELL', type='STOP_MARKET',
                                                     stopPrice=round(sl, 6), closePosition=True)
                    self.client.futures_create_order(symbol=symbol, side='SELL', type='TAKE_PROFIT_MARKET',
                                                     stopPrice=round(tp, 6), closePosition=True)
                else:
                    self.client.futures_create_order(symbol=symbol, side='BUY', type='STOP_MARKET',
                                                     stopPrice=round(sl, 6), closePosition=True)
                    self.client.futures_create_order(symbol=symbol, side='BUY', type='TAKE_PROFIT_MARKET',
                                                     stopPrice=round(tp, 6), closePosition=True)
            except Exception as e:
                print("placing tp/sl failed:", e)
            self.positions[symbol] = {
                "side": side,
                "entry": price,
                "tp": tp,
                "sl": sl,
                "qty": qty,
                "opened_at": datetime.utcnow().isoformat(),
                "orderId": order_id
            }
            rec = {"symbol": symbol, "side": side, "entry": price, "tp": tp, "sl": sl, "qty": qty,
                   "ts": datetime.utcnow().isoformat(), "note": "OPEN"}
            self.log_trade_local(rec)
            return True
        except Exception as e:
            print("open_market_position error", e)
            traceback.print_exc()
            return False

    def cancel_order_if_timeout(self, symbol):
        pos = self.positions.get(symbol)
        if not pos:
            return
        opened = datetime.fromisoformat(pos['opened_at'])
        if datetime.utcnow() - opened > timedelta(seconds=self.order_timeout):
            try:
                self.client.futures_cancel_all_open_orders(symbol=symbol)
            except Exception:
                pass
            rec = {"symbol": symbol, "ts": datetime.utcnow().isoformat(), "note": "CANCEL_TIMEOUT"}
            self.log_trade_local(rec)
            if symbol in self.positions:
                del self.positions[symbol]
            return True
        return False

    def update_trailing(self, symbol):
        pos = self.positions.get(symbol)
        if not pos or not self.trailing_enable:
            return
        try:
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            entry = float(pos['entry'])
            side = pos['side']
            if side == "BUY":
                profit_pct = (current_price - entry) / entry * 100
            else:
                profit_pct = (entry - current_price) / entry * 100
            if profit_pct >= self.trail_trigger:
                if side == "BUY":
                    new_sl = current_price * (1 - self.trail_delta/100)
                    try:
                        self.client.futures_cancel_all_open_orders(symbol=symbol)
                    except Exception:
                        pass
                    try:
                        self.client.futures_create_order(symbol=symbol,
                                                         side='SELL' if side=='BUY' else 'BUY',
                                                         type='STOP_MARKET',
                                                         stopPrice=round(new_sl, 6),
                                                         closePosition=True)
                        pos['sl'] = new_sl
                        pos['sl_updated_at'] = datetime.utcnow().isoformat()
                        self.log_trade_local({"symbol":symbol,"ts":datetime.utcnow().isoformat(),
                                              "note":"TRAILING_UPDATED","new_sl":new_sl})
                    except Exception as e:
                        print("trailing place err", e)
                else:
                    new_sl = current_price * (1 + self.trail_delta/100)
                    try:
                        self.client.futures_cancel_all_open_orders(symbol=symbol)
                    except Exception:
                        pass
                    try:
                        self.client.futures_create_order(symbol=symbol,
                                                         side='BUY' if side=='SELL' else 'SELL',
                                                         type='STOP_MARKET',
                                                         stopPrice=round(new_sl, 6),
                                                         closePosition=True)
                        pos['sl'] = new_sl
                        pos['sl_updated_at'] = datetime.utcnow().isoformat()
                        self.log_trade_local({"symbol":symbol,"ts":datetime.utcnow().isoformat(),
                                              "note":"TRAILING_UPDATED","new_sl":new_sl})
                    except Exception as e:
                        print("trailing place err", e)
        except Exception as e:
            print("update_trailing error", e)

    def fetch_user_trades(self, symbol):
        try:
            trs = self.client.futures_account_trades(symbol=symbol)
            return trs
        except Exception as e:
            print("fetch_user_trades err", e)
            return []

    def compute_realized_pnl(self, symbol, entry_price, qty, side, opened_at_iso):
        try:
            opened_at = datetime.fromisoformat(opened_at_iso)
        except Exception:
            opened_at = None
        user_trades = self.fetch_user_trades(symbol)
        recent = []
        for t in user_trades:
            ts = int(t.get("time", 0)) / 1000.0
            trade_dt = datetime.utcfromtimestamp(ts)
            if opened_at is None or trade_dt >= opened_at:
                recent.append(t)
        if not recent:
            return {"exit_price": None, "realized_pnl": None, "commission_usdt": 0.0}
        total_quote = 0.0
        total_qty = 0.0
        total_commission_usdt = 0.0
        for tr in recent:
            p = float(tr.get("price", 0.0))
            q = abs(float(tr.get("qty", 0.0)))
            total_quote += p * q
            total_qty += q
            com = float(tr.get("commission", 0.0))
            com_asset = tr.get("commissionAsset", "")
            if com_asset == "USDT":
                total_commission_usdt += com
        exit_price = (total_quote / total_qty) if total_qty > 0 else None
        if exit_price is None:
            realized = None
        else:
            if side == "BUY":
                realized = (exit_price - float(entry_price)) * float(qty)
            else:
                realized = (float(entry_price) - exit_price) * float(qty)
            realized -= total_commission_usdt
        return {"exit_price": exit_price, "realized_pnl": realized, "commission_usdt": total_commission_usdt}

    def record_realized_pnl_on_close(self, symbol):
        pos = self.positions.get(symbol)
        if not pos:
            return False
        entry_price = pos.get("entry")
        qty = pos.get("qty")
        side = pos.get("side")
        opened_at = pos.get("opened_at")
        res = self.compute_realized_pnl(symbol, entry_price, qty, side, opened_at)
        rec = {
            "symbol": symbol,
            "side": side,
            "entry": entry_price,
            "exit": res.get("exit_price"),
            "realized_pnl": res.get("realized_pnl"),
            "commission_usdt": res.get("commission_usdt"),
            "closed_at": datetime.utcnow().isoformat(),
            "note": "CLOSED_REALIZED"
        }
        self.log_trade_local(rec)
        if symbol in self.positions:
            del self.positions[symbol]
        return True

    def close_position_on_tp_sl_fill(self, symbol):
        try:
            acc = self.client.futures_account()
            for p in acc.get("positions", []):
                if p.get("symbol") == symbol:
                    amt = float(p.get("positionAmt", 0))
                    if abs(amt) < 1e-8:
                        if symbol in self.positions:
                            self.record_realized_pnl_on_close(symbol)
                        else:
                            rec = {"symbol": symbol, "ts": datetime.utcnow().isoformat(), "note": "CLOSED_UNKNOWN_TRACK"}
                            self.log_trade_local(rec)
                        return True
        except Exception as e:
            print("close check err", e)
        return False

    def run_cycle(self):
        if not self.in_trading_time():
            print("Outside trading hours.")
            return
        for s in self.pairs:
            if s in self.positions:
                closed = self.close_position_on_tp_sl_fill(s)
                if closed:
                    continue
                self.update_trailing(s)
                self.cancel_order_if_timeout(s)
                continue
            try:
                raw = self.client.futures_klines(symbol=s, interval='1h', limit=50)
                df = pd.DataFrame(raw, columns=["openTime","open","high","low","close","volume","closeTime","qav","n","tb","tq","i"])
                df['close'] = df['close'].astype(float)
                close = df['close']
                ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator()
                ema50 = ta.trend.EMAIndicator(close, window=50).ema_indicator()
                rsi = ta.momentum.RSIIndicator(close, window=14).rsi()
                latest = len(df)-1
                bullish = (ema20.iloc[latest] > ema50.iloc[latest]) and (rsi.iloc[latest] < 70)
                bearish = (ema20.iloc[latest] < ema50.iloc[latest]) and (rsi.iloc[latest] > 30)
                sig = None
                if bullish:
                    sig = "BUY"
                elif bearish:
                    sig = "SELL"
                if sig:
                    print(f"[{datetime.utcnow().isoformat()}] Signal {s}: {sig}")
                    ok = self.open_market_position(s, sig)
                    if not ok:
                        print("open position failed for", s)
                else:
                    print(f"No signal for {s}")
            except Exception as e:
                print("strategy eval err", e)

    def run_loop(self, poll_sec=30):
        print("TradeManager running. Pairs:", self.pairs)
        while True:
            try:
                self.run_cycle()
            except Exception as e:
                print("run_loop error", e)
            time.sleep(poll_sec)
