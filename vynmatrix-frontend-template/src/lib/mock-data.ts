export type StrategyStatus = "Ready" | "Draft" | "Running" | "Paused";

export const strategies = [
  { id: "STR-014", name: "EMA Momentum + RSI", market: "NSE · Equities", timeframe: "15 min", version: "v1.4.2", status: "Running" as StrategyStatus, updated: "12 min ago", return: "+8.42%", risk: "Medium" },
  { id: "STR-013", name: "Mean Reversion Alpha", market: "NSE · Equities", timeframe: "5 min", version: "v0.8.1", status: "Ready" as StrategyStatus, updated: "Yesterday", return: "+4.16%", risk: "Medium" },
  { id: "STR-012", name: "Volatility Breakout", market: "Index Futures · Paper", timeframe: "30 min", version: "v2.0.0", status: "Paused" as StrategyStatus, updated: "Oct 16, 2026", return: "-1.23%", risk: "High" },
  { id: "STR-011", name: "Trend Filter v3", market: "NSE · Equities", timeframe: "1 hour", version: "v3.0.1", status: "Draft" as StrategyStatus, updated: "Oct 15, 2026", return: "—", risk: "Not tested" },
];

export const orders = [
  { time: "14:42:08", instrument: "RELIANCE", side: "BUY", quantity: 12, type: "Market", status: "Filled", price: "₹1,468.20", strategy: "EMA Momentum + RSI" },
  { time: "14:31:54", instrument: "INFY", side: "SELL", quantity: 8, type: "Limit", status: "Rejected", price: "₹1,524.50", strategy: "EMA Momentum + RSI" },
  { time: "14:16:21", instrument: "TCS", side: "BUY", quantity: 4, type: "Market", status: "Filled", price: "₹3,112.00", strategy: "Mean Reversion Alpha" },
  { time: "13:59:07", instrument: "HDFCBANK", side: "SELL", quantity: 10, type: "Limit", status: "Pending", price: "₹954.80", strategy: "EMA Momentum + RSI" },
];

export const activity = [
  { time: "14:42:08", title: "Paper fill recorded", detail: "RELIANCE · BUY 12 units · STR-014", tone: "success" },
  { time: "14:31:54", title: "Order rejected by risk policy", detail: "INFY · Exposure limit would be exceeded", tone: "warning" },
  { time: "14:25:12", title: "Scoring decision persisted", detail: "EMA Momentum + RSI · score 0.78", tone: "info" },
  { time: "14:10:02", title: "Backtest completed", detail: "Mean Reversion Alpha · 3 years · 1,824 trades", tone: "success" },
];
