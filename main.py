from trader import TradeManager
import threading, time

if __name__ == "__main__":
    tm = TradeManager("config.yaml")
    # run trading loop (blocking)
    tm.run_loop(poll_sec=tm.cfg.get('time', {}).get('poll_sec', 30))
