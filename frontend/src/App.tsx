import { Route, Routes } from "react-router-dom";

import { Shell } from "./components/Shell";
import { ConversationLog } from "./pages/ConversationLog";
import { GroupsList } from "./pages/GroupsList";
import { PlanDetail } from "./pages/PlanDetail";
import { PlansBoard } from "./pages/PlansBoard";
import { Settings } from "./pages/Settings";

export function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<PlansBoard />} />
        <Route path="plans/:id" element={<PlanDetail />} />
        <Route path="groups" element={<GroupsList />} />
        <Route path="groups/:id" element={<ConversationLog />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<PlansBoard />} />
      </Route>
    </Routes>
  );
}
