/**
 * Telemetry service stub.
 *
 * The upstream agent-canvas SPA bundled its own analytics client; SE-3 strips
 * it for the minio-ork fork. All exports below are no-ops that keep the same
 * shape so call sites that still import the named symbols compile cleanly.
 * Local observability lives in the minio-ork backend (events/ traces) rather
 * than the SPA.
 */

export type TelemetryConfig = {
  apiKey?: string;
  apiHost?: string;
  uiHost?: string;
};

export type TelemetryConfiguration = TelemetryConfig | false;

export type TelemetryConsent = "granted" | "denied" | "pending";
export type ResolvedTelemetryConsent = Exclude<TelemetryConsent, "pending">;

export type SetTelemetryConsentOptions = {
  syncToCloud?: boolean;
};

export function setTelemetryBackendContext(_context: unknown): void {}

export function setTelemetryCloudContext(_context: unknown): void {}

export function configureTelemetry(_config: TelemetryConfiguration): void {}

export function configureAnalyticsBootstrap(_bootstrap: unknown): void {}

export function initializeAnalyticsClient(
  _enableCapturing?: boolean,
): Promise<null> {
  return Promise.resolve(null);
}

export function getTelemetryConsent(): TelemetryConsent {
  return "denied";
}

export function getPendingCloudTelemetryConsent(): ResolvedTelemetryConsent | null {
  return null;
}

export function getPendingLocalTelemetryRevocationId(): string | null {
  return null;
}

export function clearPendingLocalTelemetryRevocation(_id: string): void {}

export function clearPendingCloudTelemetryConsent(_expected?: ResolvedTelemetryConsent): void {}

export function subscribeTelemetryConsent(_listener: () => void): () => void {
  return () => {};
}

export async function setTelemetryConsent(
  _consent: ResolvedTelemetryConsent,
  _options?: SetTelemetryConsentOptions,
): Promise<void> {}

export async function setTelemetryIdentity(
  _distinctId: string | null,
  _properties: Record<string, unknown> = {},
): Promise<void> {}

export function isTelemetryEnabled(): boolean {
  return false;
}

export async function trackInstall(): Promise<void> {}

export async function trackSessionStart(): Promise<void> {}

export async function trackEvent(
  _eventName: string,
  _properties: Record<string, unknown> = {},
): Promise<void> {}

export async function trackException(
  _error: unknown,
  _properties: Record<string, unknown> = {},
): Promise<void> {}

export async function clearTelemetryData(): Promise<void> {}

export async function getTelemetryDistinctId(): Promise<string | null> {
  return null;
}

export async function getTelemetryDistinctIdForConsentSync(): Promise<string | null> {
  return null;
}
