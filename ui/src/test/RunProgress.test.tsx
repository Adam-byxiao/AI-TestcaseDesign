import {cleanup,render,screen} from '@testing-library/react';
import {afterEach,describe,expect,it} from 'vitest';
import RunProgress from '../components/RunProgress';

describe('RunProgress',()=>{
  afterEach(cleanup);

  it('explains why nothing is streamed when no metrics exist',()=>{
    render(<RunProgress/>);
    expect(screen.getByText('尚无实时指标；若后端为旧版本则不会推送')).toBeInTheDocument();
  });

  it('marks estimated tokens, shows cache reads and highlights a long silence',async()=>{
    render(<RunProgress metrics={{type:'run_metrics',stage:'cases',elapsedMs:83000,activeRequests:2,silentMs:90000,cancelled:false,finished:false,totals:{promptTokens:12031,completionTokens:5021,totalTokens:17052,cacheReadTokens:4096,cacheCreationTokens:0,estimated:true},modules:{AUTH:{state:'running',requirements:4,batch:'2/3',batchIndex:2,batchTotal:3,promptTokens:3000,completionTokens:3011,estimated:true,tokensPerSec:18.4,firstTokenMs:820,silentMs:90000,deltas:140,requests:2,preview:'AUTH 正文尾部',error:''},CART:{state:'done',requirements:2,batch:'1/1',batchIndex:1,batchTotal:1,promptTokens:1200,completionTokens:6100,estimated:false,tokensPerSec:22,firstTokenMs:610,silentMs:0,deltas:300,requests:1,preview:'CART 正文尾部',error:''}}}}/>);
    expect(await screen.findByText('耗时 1m 23s')).toBeInTheDocument();
    expect(screen.getByText('≈17052')).toBeInTheDocument();
    expect(screen.getByText('缓存读取 4096')).toBeInTheDocument();
    expect(screen.getAllByText('静默 1m 30s')).toHaveLength(2);
    expect(screen.getByText('AUTH 正文尾部')).toBeInTheDocument();
    expect(screen.getByText('≈3011')).toBeInTheDocument();
    expect(screen.getByText('6100')).toBeInTheDocument();
    expect(screen.getByText('活跃模块 2')).toBeInTheDocument();
  });
});
