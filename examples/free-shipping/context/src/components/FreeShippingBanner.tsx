import { amountLeftForFreeShipping } from "../cart/shipping";
import { formatMoney } from "../utils/money";

type Props = { subtotalCents: number };

export function FreeShippingBanner({ subtotalCents }: Props) {
  if (subtotalCents === 0) return null;

  const left = amountLeftForFreeShipping(subtotalCents);
  if (left === 0) {
    return <p className="banner banner--success">You've got free shipping!</p>;
  }

  return (
    <p className="banner">
      Add {formatMoney(left)} more to get free shipping.
    </p>
  );
}
