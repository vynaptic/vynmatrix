import Link from "next/link";
import { PageHeader, Panel } from "./ui";

export function UnavailableFeature({ title, description, backToStrategies = false }: {
  title: string; description: string; backToStrategies?: boolean;
}) {
  return <>
    <PageHeader title={title} eyebrow="UNAVAILABLE" description={description} />
    <Panel title="Backend integration unavailable">
      <p>This feature has no supported backend API yet. No data or actions are available on this page.</p>
      <Link className="button button-secondary" href={backToStrategies ? "/strategies" : "/overview"}>
        Back to {backToStrategies ? "strategies" : "overview"}
      </Link>
    </Panel>
  </>;
}
