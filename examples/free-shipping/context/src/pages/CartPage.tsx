import { useCart } from "../cart/useCart";
import { cartTotals } from "../cart/totals";
import { CartLine } from "../components/CartLine";
import { FreeShippingBanner } from "../components/FreeShippingBanner";
import { Summary } from "../components/Summary";

export function CartPage() {
  const { items } = useCart();
  const totals = cartTotals(items);

  return (
    <main className="cart">
      <h1>Your cart</h1>
      <FreeShippingBanner subtotalCents={totals.subtotal} />
      {items.map((item) => (
        <CartLine key={item.id} item={item} />
      ))}
      <Summary totals={totals} />
    </main>
  );
}
