export function Clock({ ready, hidden, entries, tonight, active, open }: ClockProps) {
  if (!hidden) open();
  const onClick = (event: Event) => open(event);
  const show = active !== tonight;
  return <main>
    {ready && !hidden ? <button onClick={onClick}>Open</button> : null}
    {show && <span>Clock</span>}
    <small>Time</small>
  </main>;
}
