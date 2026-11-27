// Orders of $50.00 or more ship for free. All money is in cents.
export const FREE_SHIPPING_MIN_CENTS = 5000;
export const STANDARD_SHIPPING_CENTS = 599;

/** What the customer pays for shipping, in cents. */
export function shippingCost(subtotalCents: number): number {
  if (subtotalCents === 0) return 0; // empty cart: nothing to ship
  return subtotalCents >= FREE_SHIPPING_MIN_CENTS ? 0 : STANDARD_SHIPPING_CENTS;
}

/** How much more the customer must add to get free shipping, in cents. */
export function amountLeftForFreeShipping(subtotalCents: number): number {
  return Math.max(0, FREE_SHIPPING_MIN_CENTS - subtotalCents);
}
