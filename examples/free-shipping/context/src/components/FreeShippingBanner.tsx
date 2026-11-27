import { FREE_SHIPPING_MIN_CENTS, type ShippingQuote } from "../cart/shipping";
import { formatMoney } from "../utils/money";

type Props = { quote: ShippingQuote };

export function FreeShippingBanner({ quote }: Props) {
  if (quote.kind !== "domestic") return null;

  if (quote.leftForFreeCents === 0) {
    return (
      <p className="banner banner--success" role="status">
        You've got free shipping!
        {quote.costCents > 0 && " Bulky items still have a delivery fee."}
      </p>
    );
  }

  const spentCents = FREE_SHIPPING_MIN_CENTS - quote.leftForFreeCents;
  const percent = Math.floor((spentCents / FREE_SHIPPING_MIN_CENTS) * 100);

  return (
    <div className="banner" role="status">
      <p>Add {formatMoney(quote.leftForFreeCents)} more to get free shipping.</p>
      <div
        className="banner__bar"
        role="progressbar"
        aria-label="Progress to free shipping"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
      >
        <div className="banner__fill" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}
