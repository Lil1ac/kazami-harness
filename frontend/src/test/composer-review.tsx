
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { ConversationPanel } from '../components/ConversationPanel';
import { ConfigDraftProvider } from '../config';
import { ThemeProvider } from '../theme';
import { makeRun, makeConversationManagement } from './fixtures';
import '../styles.css';
// 临时本机视觉夹具，只读静态响应；不连接服务或调用模型。
const params = new URLSearchParams(location.search);
if (!params.has('frame')) {
  createRoot(document.getElementById('root')!).render(<main style={{padding:24,display:'flex',gap:24,flexWrap:'wrap',background:'#ddd',minHeight:'100vh'}}>
    {['light','dark'].map(theme => [752,390].map(width => <iframe title={theme+width}
      style={{width,height:480,border:0}} src={'?frame=1&theme='+theme+'&width='+width} />))}
  </main>);
} else {
  localStorage.setItem('harness.appearance', params.get('theme') || 'light');
  const run = makeRun({active:true,phase:'model'});
  run.result.execution_status='running';
  const conversation = {title:'输入区验收', session_id:'run-a',latest_run_id:'run-a',
    management:makeConversationManagement(),can_continue:false,blocked_code:'run_busy',
    blocked_reason:null,active_run_ids:['run-a'],turns:[]};
  const runtime={active:true,state:{run_id:'run-a',session_id:'run-a',status:'running',messages:[],checkpoint:null,finished_at:null},commands:[]};
  window.fetch=async (input) => {
    const url=String(input);
    const data=url.includes('/runtime') ? runtime : url.includes('/tasks') ? {revision:0,items:[]} : conversation;
    return new Response(JSON.stringify(data),{headers:{'content-type':'application/json'}});
  };
  const cache=new QueryClient({defaultOptions:{queries:{retry:false}}});
  createRoot(document.getElementById('root')!).render(<ThemeProvider><QueryClientProvider client={cache}>
    <MemoryRouter><ConfigDraftProvider><div style={{height:480,display:'flex',flexDirection:'column'}}>
      <ConversationPanel run={run} conversation={conversation} events={[]} onInspect={()=>{}} onConfirmed={()=>{}} onStop={()=>{}} />
    </div></ConfigDraftProvider></MemoryRouter>
  </QueryClientProvider></ThemeProvider>);
}
