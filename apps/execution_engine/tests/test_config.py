from execution_engine.config import UserExecutionConfig


def test_user_execution_config_round_trips_notification_and_extended_fields() -> None:
    payload = {
        "user_id": "user-1",
        "strategy_id": "swing_high_low_pmo_v1",
        "broker": "paper",
        "execution_mode": "spot",
        "sizing": {
            "method": "fixed_pct",
            "fixed_amount": 2500.0,
            "fixed_pct": 0.03,
            "risk_pct": 0.02,
            "max_position_pct": 0.15,
            "min_position_size": 25.0,
            "kelly_fraction": 0.5,
        },
        "options": {
            "days_to_expiry": 21,
            "days_to_expiry_min": 10,
            "days_to_expiry_max": 35,
            "strike_offset_pct": 0.07,
            "spread_width_pct": 0.12,
            "max_bid_ask_spread": 0.08,
            "min_open_interest": 250,
            "min_delta": 0.25,
            "max_delta": 0.45,
        },
        "futures": {
            "contract_size": 5.0,
            "max_leverage": 8.0,
            "target_leverage": 3.0,
            "use_perpetual": False,
        },
        "notification": {
            "webhook_url": "https://alerts.example.test/hook",
            "email": "ops@example.test",
            "telegram_chat_id": "12345",
            "include_reasoning": False,
        },
        "max_daily_trades": 12,
        "max_open_positions": 4,
        "max_portfolio_risk_pct": 0.18,
        "enable_shorting": False,
        "require_stop_loss": True,
        "auto_close_on_exit_signal": False,
    }

    config = UserExecutionConfig.from_dict(payload)

    assert config.to_dict() == payload
