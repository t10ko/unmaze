import type { CartItem } from "./types";
import { couponDiscount, type Coupon } from "./coupons";
import { shippingQuote } from "./shipping";

export function subtotal(items: CartItem[]): number {
  return items.reduce((sum, item) => sum + item.priceCents * item.quantity, 0);
}

export function cartTotals(items: CartItem[], coupon: Coupon | null, country = "US") {
  const sub = subtotal(items);
  const discount = couponDiscount(items, coupon);
  const quote = shippingQuote(items, coupon, country);
  const shipping = quote.kind === "nothing_to_ship" ? 0 : quote.costCents;
  return { subtotal: sub, discount, shipping, total: sub - discount + shipping, quote };
}
