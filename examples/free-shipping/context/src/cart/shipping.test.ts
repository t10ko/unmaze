import type { CartItem } from "./types";
import { shippingQuote } from "./shipping";

const item = (id: string, priceCents: number, extra: Partial<CartItem> = {}): CartItem => ({
  id,
  name: id,
  priceCents,
  quantity: 1,
  kind: "product",
  ...extra,
});

const tote = item("tote", 2250);
const mug = item("mug", 1500);
const lamp = item("lamp", 4000, { bulkyFeeCents: 1500 });
const giftCard = item("gift-card", 5000, { kind: "gift_card" });

describe("shippingQuote", () => {
  it("charges $5.99 and says how much is left under $50", () => {
    expect(shippingQuote([tote, mug], null, "US")).toEqual({
      kind: "domestic",
      costCents: 599,
      leftForFreeCents: 1250,
    });
  });

  it("is free at exactly $50", () => {
    expect(shippingQuote([item("rug", 5000)], null, "US")).toMatchObject({ costCents: 0 });
  });

  it("checks the $50 after the coupon comes off", () => {
    const quote = shippingQuote([item("jacket", 5500)], { percentOff: 10 }, "US");
    expect(quote).toMatchObject({ costCents: 599, leftForFreeCents: 50 });
  });

  it("does not count gift cards toward the $50", () => {
    expect(shippingQuote([mug, giftCard], null, "US")).toMatchObject({ leftForFreeCents: 3500 });
  });

  it("still charges the bulky fee when shipping is free", () => {
    expect(shippingQuote([lamp, tote], null, "US")).toMatchObject({ costCents: 1500 });
  });

  it("uses the flat international rate abroad, even over $50", () => {
    expect(shippingQuote([lamp, tote], null, "DE")).toEqual({ kind: "international", costCents: 3499 });
  });

  it("has nothing to ship when the cart is only gift cards", () => {
    expect(shippingQuote([giftCard], null, "US")).toEqual({ kind: "nothing_to_ship" });
  });
});
