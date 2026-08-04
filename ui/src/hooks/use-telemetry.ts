import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import {
  getTelemetryConsent,
  setTelemetryConsent,
  trackInstall,
  trackSessionStart,
  trackEvent,
  clearTelemetryData,
  subscribeTelemetryConsent,
  type TelemetryConsent,
} from "#/services/telemetry";

export interface UseTelemetryReturn {
  consent: TelemetryConsent;
  isEnabled: boolean;
  showConsentPrompt: boolean;
  grantConsent: () => Promise<void>;
  denyConsent: () => Promise<void>;
  track: (eventName: string, properties?: Record<string, unknown>) => void;
  clearData: () => Promise<void>;
}

// Stub: the upstream agent-canvas bundled an analytics client; SE-3 strips it
// for the minio-ork fork. Calls no-op while keeping the same surface so the
// consent banner + tracking hooks still compile and render.
export function useTelemetry(): UseTelemetryReturn {
  const consent = useSyncExternalStore<TelemetryConsent>(
    subscribeTelemetryConsent,
    getTelemetryConsent,
    () => "pending",
  );

  const hasTrackedInstall = useRef(false);
  useEffect(() => {
    if (!hasTrackedInstall.current) {
      hasTrackedInstall.current = true;
      void trackInstall();
    }
  }, []);

  useEffect(() => {
    if (consent === "granted") {
      void trackSessionStart();
    }
  }, [consent]);

  const grantConsent = useCallback(() => setTelemetryConsent("granted"), []);
  const denyConsent = useCallback(() => setTelemetryConsent("denied"), []);

  const track = useCallback(
    (eventName: string, properties?: Record<string, unknown>) => {
      void trackEvent(eventName, properties);
    },
    [],
  );

  const clearData = useCallback(() => clearTelemetryData(), []);

  return {
    consent,
    isEnabled: consent === "granted",
    showConsentPrompt: false,
    grantConsent,
    denyConsent,
    track,
    clearData,
  };
}
