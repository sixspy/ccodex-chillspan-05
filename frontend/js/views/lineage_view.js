export const lineageViewTemplate = `
<section class="panel">
  <h2>冷链血缘</h2>
  <p class="hint">跨代血缘纪要·甲路径（当班封死）：任一端点已封代，正式血缘边零增并拒收；同对端点抢血缘锁，后到留争用痕不落边。</p>
  <div class="form-row">
    <div><label>来源批次ID</label><input type="number" v-model.number="lineageForm.from_batch_id"/></div>
    <div><label>目标批次ID</label><input type="number" v-model.number="lineageForm.to_batch_id"/></div>
    <div><label>类型</label><input v-model="lineageForm.edge_type"/></div>
    <div><label>说明</label><input v-model="lineageForm.detail"/></div>
  </div>
  <div class="actions"><button @click="addLineageEdge">登记血缘边</button></div>
  <table class="data-table">
    <thead><tr><th>自</th><th>自代</th><th>至</th><th>至代</th><th>类型</th><th>状态</th><th>说明</th><th>时间</th></tr></thead>
    <tbody>
      <tr v-for="e in lineageEdges" :key="e.id">
        <td>{{ e.from_code }}</td>
        <td>{{ e.from_generation || "—" }}</td>
        <td>{{ e.to_code || "—" }}</td>
        <td>{{ e.to_generation || "—" }}</td>
        <td>{{ e.edge_type }}</td>
        <td><span class="badge pass">{{ e.status || "正式" }}</span></td>
        <td>{{ e.detail }}</td>
        <td>{{ e.created_at }}</td>
      </tr>
    </tbody>
  </table>
</section>
`;
