import { useRoutes, tabs } from "./navigation-routes";
import * as routes from "./navigation-routes";

export function Panel() {
  const router = useRoutes();
  const unused = <button onClick={() => history.back()}>Unrendered</button>;
  return <>
    <Tabs navigate={router.navigate} />
    <Schedule dismiss={router.dismiss} />
    <button onClick={() => history.pushState({}, "", "#/list")}>List</button>
    <button onClick={router.unresolved}>Details</button>
    <button onClick={() => history.go(-1)}>Return</button>
    <button onClick={() => false && history.back()}>Continue</button>
    <button onClick={routes.previous}>Previous</button>
  </>;
}

function Tabs({ navigate }) {
  return <nav>{tabs.map(tab =>
    <button role="tab" onClick={() => navigate(`/${tab.toLowerCase()}`)}>{tab}</button>)}</nav>;
}

function Schedule({ dismiss }) {
  return <Modal label="Schedule" close={dismiss} />;
}

function Modal({ label, close }) {
  return <div onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <section role="dialog" aria-label={label}><h2>Schedule</h2></section>
  </div>;
}
