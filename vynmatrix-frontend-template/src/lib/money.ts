type Decimal = { units: bigint; scale: number };
type MoneyEntry = { currency: string | null | undefined; value: string | null | undefined };

// The API serializes Decimal values as plain strings. Keep them exact during display and sums.
function decimal(value: string | null | undefined): Decimal | null {
  const match = value?.trim().match(/^([+-]?)(\d+)(?:\.(\d*))?$/);
  if (!match) return null;
  const fraction = (match[3] ?? "").replace(/0+$/, "");
  const units = BigInt(`${match[1]}${match[2]}${fraction}`);
  return { units, scale: fraction.length };
}

function decimalString({ units, scale }: Decimal): string {
  const negative = units < BigInt(0);
  const digits = (negative ? -units : units).toString().padStart(scale + 1, "0");
  const value = scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits;
  return `${negative ? "-" : ""}${value}`;
}

export function formatAmount(value: string | null | undefined, signed = false): string {
  const amount = decimal(value);
  if (!amount) return "Unavailable";
  const normalized = decimalString(amount);
  const negative = amount.units < BigInt(0);
  const [integer, fraction] = (negative ? normalized.slice(1) : normalized).split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : signed ? "+" : ""}${grouped}${fraction ? `.${fraction}` : ""}`;
}

export function formatMoney(
  value: string | null | undefined,
  currency: string | null | undefined,
  signed = false,
): string {
  const formatted = formatAmount(value, signed);
  if (formatted === "Unavailable") return formatted;
  if (!currency?.trim()) return `${formatted} · Currency unavailable`;
  const sign = /^[+-]/.test(formatted) ? formatted[0] : "";
  return `${sign}${currency} ${sign ? formatted.slice(1) : formatted}`;
}

export function formatMoneyTotals(entries: readonly MoneyEntry[] | null, signed = false): string {
  if (!entries?.length) return "Unavailable";
  const totals = new Map<string, Decimal | null>();
  for (const entry of entries) {
    const currency = entry.currency;
    if (!currency?.trim()) return "Unavailable";
    const amount = decimal(entry.value);
    const previous = totals.get(currency);
    if (!amount || previous === null) {
      totals.set(currency, null);
    } else if (!previous) {
      totals.set(currency, amount);
    } else {
      const scale = Math.max(previous.scale, amount.scale);
      totals.set(currency, {
        units: previous.units * BigInt(10) ** BigInt(scale - previous.scale)
          + amount.units * BigInt(10) ** BigInt(scale - amount.scale),
        scale,
      });
    }
  }
  return Array.from(totals, ([currency, amount]) => amount
    ? formatMoney(decimalString(amount), currency, signed)
    : `${currency} Unavailable`).join(" · ");
}
