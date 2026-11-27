import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Panel } from "./navigation-panel";
import { click, clickOutside, tab } from "./navigation-events";

it("returns to the first tab", () => {
  render(<Panel />);
  fireEvent.click(tab("Calendar"));
  fireEvent.click(tab("Desk"));
  expect(location.hash).toBe("#/desk");
});

it("dismisses an overlay without changing the route", () => {
  render(<Panel />);
  fireEvent.click(screen.getByRole("dialog", { name: "Schedule" }).querySelector("h2")!);
  clickOutside("Schedule");
  expect(location.hash).toBe("#/calendar");
});

it("uses native history directly", () => {
  render(<Panel />);
  click("Return");
  expect(history.state).toEqual(null);
});

it("switches secondary tabs", () => {
  render(<Panel />);
  for (const name of ["Calendar", "Archive"]) fireEvent.click(tab(name));
  expect(location.hash).toBe("#/archive");
});

it("opens a list or a detail", () => {
  render(<Panel />);
  click("List");
  expect(location.hash).toBe("#/list");
  click("Details");
  expect(location.hash).toBe("#/detail");
});

it("does not infer a handler from a nearby back call", () => {
  render(<Panel />);
  click("Open filters");
  expect(location.hash).toBe("#/filters");
  click("Pick item");
  expect(location.hash).toBe("#/picked");
  click("Open item");
  expect(location.hash).toBe("#/item");
  click("Choose item");
  expect(location.hash).toBe("#/chosen");
  click("Open picker");
  expect(location.hash).toBe("#/picker");
});

it("waits for the route", async () => {
  render(<Panel />);
  fireEvent.click(tab("Desk"));
  await waitFor(() => expect(location.hash).toBe("#/desk"));
  expect(history.state).toEqual(null);
});

it("uses the same tab router without traversing history", () => {
  render(<Panel />);
  fireEvent.click(tab("Archive"));
  expect(location.hash).toBe("#/archive");
});

it("ignores unreachable traversal and unused controls", () => {
  render(<Panel />);
  click("Continue");
  expect(location.hash).toBe("#/next");
  click("Unrendered");
  expect(location.hash).toBe("#/unknown");
});

it("uses a function from a namespace import", () => {
  render(<Panel />);
  click("Previous");
  expect(location.hash).toBe("#/previous");
});
