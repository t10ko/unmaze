import { amountLeftForFreeShipping, shippingCost } from "./shipping";

describe("shippingCost", () => {
  it("charges $5.99 under $50", () => {
    expect(shippingCost(4999)).toBe(599);
  });

  it("is free at exactly $50", () => {
    expect(shippingCost(5000)).toBe(0);
  });

  it("is free for an empty cart", () => {
    expect(shippingCost(0)).toBe(0);
  });
});

describe("amountLeftForFreeShipping", () => {
  it("says how much is left", () => {
    expect(amountLeftForFreeShipping(3750)).toBe(1250);
  });

  it("never goes below zero", () => {
    expect(amountLeftForFreeShipping(8000)).toBe(0);
  });
});
