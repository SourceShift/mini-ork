import React from "react";
import { useTelemetry } from "#/hooks/use-telemetry";
import {
  configureAnalyticsBootstrap,
  configureTelemetry,
  initializeAnalyticsClient,
  type TelemetryConfiguration,
} from "#/services/telemetry";

// Stub: the upstream agent-canvas SPA booted a third-party analytics client
// here. The minio-ork fork defers analytics to the backend (events/ traces)
// and lets the consent banner flow through the no-op telemetry service.

function TelemetryLifecycle() {
  useTelemetry();
  return null;
}

export function TelemetryProvider({
  children,
  config = {},
}: {
  children: React.ReactNode;
  config?: TelemetryConfiguration;
}) {
  const configuredBootstrap = React.useRef(false);
  const analyticsEnabled = config !== false;
  const apiKey = config === false ? undefined : config.apiKey;
  const apiHost = config === false ? undefined : config.apiHost;
  const uiHost = config === false ? undefined : config.uiHost;

  React.useLayoutEffect(() => {
    configureTelemetry(analyticsEnabled ? { apiKey, apiHost, uiHost } : false);
    if (!configuredBootstrap.current) {
      configureAnalyticsBootstrap(undefined);
      configuredBootstrap.current = true;
    }
  }, [analyticsEnabled, apiHost, apiKey, uiHost]);

  React.useEffect(() => {
    if (analyticsEnabled) {
      void initializeAnalyticsClient().catch(() => {
        // Analytics are optional; the service retries on the next operation.
      });
    }
  }, [analyticsEnabled, apiHost, apiKey, uiHost]);

  return (
    <>
      {analyticsEnabled ? <TelemetryLifecycle /> : null}
      {children}
    </>
  );
}
