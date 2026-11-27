export const tabs = ["Desk", "Calendar", "Archive"] as const;
export function useRoutes() {
  function navigate(next: string) {
    if (history.state?.modal && next === location.pathname) {
      history.back();
      return;
    }
    if (history.state?.tab && next === "/desk") {
      history.back();
      return;
    }
    history.replaceState({}, "", `#${next}`);
  }
  return {
    navigate,
    dismiss() {
      if (history.state?.modal) window.history.back();
    },
  };
}

export function previous() {
  history.go(-1);
}
