import type { CartItem } from "./types";
import { shippingCost } from "./shipping";

export function subtotal(items: CartItem[]): number {
  return items.reduce((sum, item) => sum + item.priceCents * item.quantity, 0);
}

export function cartTotals(items: CartItem[]) {
  const sub = subtotal(items);
  const shipping = shippingCost(sub);
  return { subtotal: sub, shipping, total: sub + shipping };
}
