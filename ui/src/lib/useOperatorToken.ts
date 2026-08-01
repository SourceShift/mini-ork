import { useSyncExternalStore } from "react";

import { getOperatorToken, subscribeOperatorToken } from "./api";

/** Subscribe a component to the operator-token store. Re-renders whenever the
 * token is set or cleared anywhere in the app, so privileged controls can gate
 * themselves on `token != null` without prop-drilling or context. */
export function useOperatorToken(): string | null {
  return useSyncExternalStore(subscribeOperatorToken, getOperatorToken, getOperatorToken);
}
