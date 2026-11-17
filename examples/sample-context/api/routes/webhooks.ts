/**
 * External Webhook Ingestion Routes
 * Handles signed callbacks from Stripe, GitHub, and Adyen.
 */
import type { Request, Response, NextFunction } from "express";
import { logger } from "../../lib/logger";
import { metrics } from "../../lib/metrics";
import { WebhookSignatureError, verifyWebhookSignature } from "../../services/webhooks/signature";

const SUPPORTED_PROVIDERS = new Set(["stripe", "github", "adyen"]);

import { Router } from "express";
import { container } from "../../config/container";

export const webhookRouter = Router();

webhookRouter.post("/webhooks/:provider", async (req, res) => {
  const { provider } = req.params;
  const eventId = req.header("x-webhook-id") ?? req.body?.id;
  if (!eventId) {
    return res.status(400).json({ error: "Missing webhook event ID" });
  }

  const decision = await container.idempotencyStore.claimOrSkip(provider, eventId);
  if (decision.action !== "PROCESS") {
    return res.status(200).json({ deduplicated: true, reason: decision.action });
  }

  try {
    await container.webhookDispatcher.dispatch(provider, req.body);
    await container.idempotencyStore.markCompleted(provider, eventId);
    return res.status(200).json({ ok: true });
  } catch (err) {
    await container.idempotencyStore.releaseClaim(provider, eventId);
    throw err;
  }
});

webhookRouter.get("/webhooks/health", (_req: Request, res: Response) => {
  res.status(200).json({
    status: "healthy",
    providers: Array.from(SUPPORTED_PROVIDERS),
  });
});

export function webhookErrorHandler(
  err: unknown,
  _req: Request,
  res: Response,
  next: NextFunction,
) {
  if (err instanceof WebhookSignatureError) {
    metrics.increment("webhooks.invalid_signature");
    return res.status(401).json({ error: "Invalid webhook signature" });
  }
  logger.error({ err }, "Unhandled webhook processing error");
  return next(err);
}

export function isSupportedWebhookProvider(provider: string): boolean {
  return SUPPORTED_PROVIDERS.has(provider.toLowerCase());
}

