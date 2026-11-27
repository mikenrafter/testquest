import { fireEvent, screen } from "@testing-library/react";
export function tab(name: string) {
  return screen.getByRole("tab", { name });
}
export function click(name: string) {
  fireEvent.click(screen.getByRole("button", { name }));
}
export function clickOutside(name: string) {
  const overlay = screen.getByRole("dialog", { name }).parentElement!;
  fireEvent.click(overlay);
}
