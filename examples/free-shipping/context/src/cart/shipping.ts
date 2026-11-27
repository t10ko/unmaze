import type { CartItem } from "./types";
import { couponDiscount, type Coupon } from "./coupons";

// Orders of $50.00 or more ship free inside the US. All money is in cents.
export const FREE_SHIPPING_MIN_CENTS = 5000;
export const STANDARD_SHIPPING_CENTS = 599;
export const INTERNATIONAL_SHIPPING_CENTS = 1999;

export type ShippingQuote =
  | { kind: "nothing_to_ship" }
  | { kind: "international"; costCents: number }
  | { kind: "domestic"; costCents: number; leftForFreeCents: number };

/** What counts toward the $50: physical items only, after the coupon. */
export function eligibleSubtotalCents(items: CartItem[], coupon: Coupon | null): number {
  const physical = items.filter((item) => item.kind !== "gift_card");
  const merchandise = physical.reduce((sum, item) => sum + item.priceCents * item.quantity, 0);
  return merchandise - couponDiscount(physical, coupon);
}

export function shippingQuote(
  items: CartItem[],
  coupon: Coupon | null,
  country: string,
): ShippingQuote {
  // Gift cards are emailed, so a cart of only gift cards ships nothing.
  if (!items.some((item) => item.kind !== "gift_card")) {
    return { kind: "nothing_to_ship" };
  }

  // Bulky items pay their own delivery fee, even when shipping is free.
  const bulkyFees = items.reduce(
    (sum, item) => sum + (item.bulkyFeeCents ?? 0) * item.quantity,
    0,
  );

  if (country !== "US") {
    return { kind: "international", costCents: INTERNATIONAL_SHIPPING_CENTS + bulkyFees };
  }

  const leftForFreeCents = Math.max(0, FREE_SHIPPING_MIN_CENTS - eligibleSubtotalCents(items, coupon));
  const base = leftForFreeCents === 0 ? 0 : STANDARD_SHIPPING_CENTS;
  return { kind: "domestic", costCents: base + bulkyFees, leftForFreeCents };
}
