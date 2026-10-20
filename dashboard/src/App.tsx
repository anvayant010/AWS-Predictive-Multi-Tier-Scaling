import { Arena } from "./components/Arena";
import { AwsPlaceholder, ComparisonPlaceholder } from "./components/Placeholders";
import { Footer } from "./components/Footer";
import { Hero } from "./components/Hero";
import { Section } from "./components/Section";
import { TopBar } from "./components/TopBar";
import { useDashboardData } from "./hooks/useDashboardData";
import { useTheme } from "./hooks/useTheme";

export default function App() {
  const { theme, toggle } = useTheme();
  const state = useDashboardData();

  return (
    <>
      <TopBar theme={theme} onToggleTheme={toggle} />
      <main>
        {state.status === "loading" && (
          <div className="gate" role="status">
            <h1>Loading the recorded run…</h1>
          </div>
        )}
        {state.status === "error" && (
          <div className="gate" role="alert">
            <h1>The data could not be loaded</h1>
            <p>{state.message}</p>
          </div>
        )}
        {state.status === "ready" && (
          <>
            <Hero comparison={state.data.comparison} />
            <Arena data={state.data} />
            <Section
              id="versus"
              kicker="02 · Comparison"
              title="Against the reactive baseline"
              note="Same traffic, same AWS delays, same one-minute metric lag for every approach. Dots are medians; whiskers span the seeds."
            >
              <ComparisonPlaceholder data={state.data} />
            </Section>
            <Section
              id="measured"
              kicker="03 · Real AWS"
              title="What the clock really looks like"
              note="Not simulated: measured on a deployed stack. These are the delays the replay uses."
            >
              <AwsPlaceholder data={state.data} />
            </Section>
            <Footer meta={state.data.meta} />
          </>
        )}
      </main>
    </>
  );
}
