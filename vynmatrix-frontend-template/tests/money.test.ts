import { describe, expect, it } from "vitest";
import { formatAmount, formatMoney, formatMoneyTotals } from "@/lib/money";

describe("monetary display", () => {
  it("preserves recorded decimal precision and signs without numeric conversion", () => {
    expect(formatAmount("9007199254740993.123456789")).toBe("9,007,199,254,740,993.123456789");
    expect(formatMoney("-2.2500", "EUR", true)).toBe("-EUR 2.25");
    expect(formatMoney("0.000", "CHF", true)).toBe("+CHF 0");
  });

  it("sums decimal strings within currency and preserves missing totals", () => {
    expect(formatMoneyTotals([
      { currency: "EUR", value: "0.1" }, { currency: "EUR", value: "0.2" },
      { currency: "USD", value: "9007199254740993.123456789" },
    ], true)).toBe("+EUR 0.3 · +USD 9,007,199,254,740,993.123456789");
    expect(formatMoneyTotals([
      { currency: "EUR", value: "12" }, { currency: "EUR", value: null },
      { currency: "GBP", value: "0" },
    ])).toBe("EUR Unavailable · GBP 0");
  });

  it("does not fabricate money for missing, invalid, or non-finite data", () => {
    for (const value of [null, undefined, "", "NaN", "Infinity", "not-money"]) {
      expect(formatAmount(value)).toBe("Unavailable");
      expect(formatMoney(value, "EUR")).toBe("Unavailable");
    }
    expect(formatMoney("2.5", null)).toBe("2.5 · Currency unavailable");
    expect(formatMoneyTotals(null)).toBe("Unavailable");
    expect(formatMoneyTotals([])).toBe("Unavailable");
  });
});
