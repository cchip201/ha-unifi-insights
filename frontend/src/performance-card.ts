import {
    PERFORMANCE_CARD_TAG,
    PERFORMANCE_EDITOR_TAG,
    UnifiInsightsPerformanceCard,
    UnifiInsightsPerformanceCardEditor,
} from "./dashboard-cards";
import { registerDashboardCard } from "./register-dashboard-card";

registerDashboardCard({
    tag: PERFORMANCE_CARD_TAG,
    editorTag: PERFORMANCE_EDITOR_TAG,
    card: UnifiInsightsPerformanceCard,
    editor: UnifiInsightsPerformanceCardEditor,
    name: "UniFi Device Performance",
    description: "Infrastructure CPU, memory, PoE, client load, and throughput summary.",
});

export {
    PERFORMANCE_CARD_TAG,
    PERFORMANCE_EDITOR_TAG,
    UnifiInsightsPerformanceCard,
    UnifiInsightsPerformanceCardEditor,
};
