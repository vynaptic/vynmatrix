/**
 * TypeScript types matching the Vynmatrix backend API responses
 */

export interface OverviewData {
  as_of: string;
  safety: {
    execution_mode: string;
    allow_live: boolean;
    environment: string;
  };
  owner: {
    display_name: string;
    base_currency: string;
    timezone: string;
  };
  accounts: Array<{
    account_id: number;
    name: string;
    environment: string;
    currency: string;
    status: string;
    starting_equity: string | null;
    equity: {
      value: string | null;
      as_of: string | null;
      source: string;
    } | null;
    equity_trend: string[] | null;
    realized_pnl: {
      value: string | null;
      as_of: string | null;
    } | null;
    open_positions: number | null;
    fills: number | null;
  }>;
  strategies: {
    catalogue: number;
    released: number;
    bound: number;
    active: number;
  } | null;
  freshness: {
    price_feeds: Array<{
      timeframe: string;
      last_at: string | null;
    }> | null;
    last_signal_at: string | null;
    last_fill_at: string | null;
  };
  activity: Array<{
    id?: number;
    kind: "signal" | "fill";
    at: string;
    strategy_id?: string;
    symbol?: string;
    action?: string;
    confidence?: string;
    account_id?: number;
    side?: string;
    quantity?: string;
    price?: string;
    currency?: string;
    fee?: string;
    fee_currency?: string;
    venue?: string;
  }>;
}

export interface StrategiesData {
  as_of: string;
  strategies: Array<{
    strategy_id: string;
    name: string;
    asset_class: string;
    description: string;
    released: boolean;
    version: string | null;
    status: string | null;
    bindings: Array<{
      binding_id: number;
      account_id: number;
      account_name: string | null;
      mode: string;
      active: boolean;
      autopilot: boolean;
      entries_enabled: boolean;
      exits_enabled: boolean;
    }>;
    last_signal: {
      at: string | null;
      action: string | null;
      symbol: string | null;
    } | null;
    realized_pnl: Array<{
      account_id: number;
      currency: string;
      value: string;
    }> | null;
  }>;
}

export interface PnLData {
  as_of: string;
  days: number;
  accounts: Array<{
    account_id: number;
    name: string;
    currency: string;
    starting_equity: string | null;
    equity: {
      value: string | null;
      as_of: string | null;
      source: string;
    } | null;
    equity_daily: Array<{
      date: string;
      value: string;
    }> | null;
    equity_by_trade: Array<{
      trade: number;
      at: string;
      value: string;
    }> | null;
    realized_total: string | null;
    realized: Array<{
      strategy_id: string;
      symbol: string;
      value: string;
      as_of: string;
    }> | null;
    positions: Array<{
      symbol: string;
      quantity: string;
      average_price: string | null;
      last_mark: string | null;
      notional: string | null;
      notional_currency: string | null;
      as_of: string;
    }> | null;
    fees: Array<{
      currency: string;
      value: string;
    }> | null;
  }>;
}

export interface ControlData {
  as_of: string;
  owner: {
    full_name: string | null;
    base_ccy: string;
    tz: string;
  } | null;
  accounts: Array<{
    account_id: number;
    config_key: string;
    display_name: string;
    broker_code: string;
    broker_name: string;
    environment: string;
    base_ccy: string;
    status: string;
    external_ref: string | null;
    paper_initial_equity: string | null;
    paper_initial_cash: string | null;
    credential: {
      present: boolean;
      status: string | null;
      expires_at: string | null;
    };
  }> | null;
  bindings: Array<{
    binding_id: number;
    strategy_id: string;
    account_id: number;
    mode: string;
    asset_score_threshold: string;
    max_position_pct: string;
    max_total_exposure_pct: string;
    max_daily_loss_pct: string;
    max_open_positions: number;
    instruments_allowed: string[] | null;
    asset_classes_allowed: string[];
  }> | null;
  strategies: Array<{
    strategy_id: string;
    name: string;
    asset_class: string;
    released: boolean;
  }>;
  activity: Array<{
    at: string;
    action: string;
    status: string;
    fields: string[];
  }> | null;
  modes: string[];
  limits: Record<string, {
    min: string;
    max: string;
    exclusive_min: boolean;
  }>;
}

export interface VersionData {
  as_of: string;
  deployment: {
    deployment_id: number;
    mode: string;
    image_tag: string;
    source_commit: string;
    source_dirty: boolean;
    alembic_head: string;
    started_at: string;
    finished_at: string;
    outcome: string;
  } | null;
  image: {
    source_commit: string | null;
    source_dirty: string | null;
    build_time: string | null;
    base_image: string | null;
  } | null;
  drift: boolean | null;
}

export interface FillsData {
  as_of: string;
  fills: Array<{
    id: number;
    at: string;
    account_id: number;
    strategy_id: string | null;
    symbol: string;
    side: string;
    quantity: string;
    price: string;
    currency: string;
    fee: string;
    fee_currency: string;
    venue: string;
  }>;
  next_before: string | null;
}
