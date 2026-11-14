"use strict";
// COPY-only final-total observation model. No inferred rank is called observed.
// The original core still owns next-hand simulation, EV, ROAD_AUX and gates.
const core=require("./soft_shoe_decision_runtime.js");
const VERSION="BBB_SOFT_SHOE_TOTALS_EV_V1";
const OBSERVATION_MODEL="final_totals_importance_v1";
const BASE=[128,32,32,32,32,32,32,32,32,32],TOTAL=416;
const sum=a=>a.reduce((x,y)=>x+y,0);
const outcome=s=>s.banker>s.player?"B":s.player>s.banker?"P":"T";
function validateScores(history,scores){
 if(!Array.isArray(history)||history.some(x=>!["B","P","T"].includes(x)))throw Error("invalid_bpt_history");
 if(!Array.isArray(scores)||scores.length!==history.length)throw Error("scores_history_length_mismatch");
 return scores.map((s,i)=>{
  if(s===null)return null; // Explicitly unknown; never fabricate old totals.
  if(!s||typeof s!=="object"||Array.isArray(s)||
     Object.keys(s).sort().join(",")!=="banker,player"||
     ![s.player,s.banker].every(x=>Number.isInteger(x)&&x>=0&&x<=9))throw Error("invalid_final_totals");
  if(outcome(s)!==history[i])throw Error("scores_outcome_mismatch");
  return {player:s.player,banker:s.banker};
 });
}
function rngFrom(seed){
 let x=seed>>>0||0x9e3779b9;
 return()=>{x^=x<<13;x^=x>>>17;x^=x<<5;return(x>>>0)/4294967296;};
}
function bankDraw(b,p3){
 return p3===null?b<=5:b<=2?true:b===3?p3!==8:
  b===4?p3>=2&&p3<=7:b===5?p3>=4&&p3<=7:b===6?p3===6||p3===7:false;
}
const cache=new Map();
function branchesFor(score){
 const key=score.player*10+score.banker;
 if(cache.has(key))return cache.get(key);
 const branches=[];
 // A final pair and the two initial totals uniquely determine third cards.
 // Natural 8/9 terminates BOTH hands, including a non-natural opponent.
 for(let p=0;p<10;p++)for(let b=0;b<10;b++){
  let p3=null,b3=null;
  if(p>=8||b>=8){if(p!==score.player||b!==score.banker)continue;}
  else{
   if(p<=5)p3=(score.player-p+10)%10;
   else if(p!==score.player)continue;
   if(bankDraw(b,p3))b3=(score.banker-b+10)%10;
   else if(b!==score.banker)continue;
  }
  branches.push(Object.freeze({p,b,p3,b3}));
 }
 const result=Object.freeze(branches);cache.set(key,result);return result;
}
function propose(counts,score,rng,branches=branchesFor(score)){
 const available=sum(counts);
 if(available<6||!branches.length)return null;
 const left=counts.slice(),cards=[];
 let total=available,weight=branches.length;
 const branch=branches[Math.floor(rng()*branches.length)];
 function draw(){
  let u=rng()*total;
  for(let r=0;r<10;r++)if((u-=left[r])<0){left[r]--;total--;cards.push(r);return r;}
  throw Error("score_proposal_exhausted");
 }
 function force(r){
  if(left[r]<=0)return false;
  weight*=left[r]/total;left[r]--;total--;cards.push(r);return true;
 }
 const p1=draw(),b1=draw();
 if(!force((branch.p-p1+10)%10)||!force((branch.b-b1+10)%10)||
    (branch.p3!==null&&!force(branch.p3))||
    (branch.b3!==null&&!force(branch.b3)))return null;
 // q chooses one of K branches uniformly, then samples P1/B1 physically.
 // p(path)/q(path) = K * product of the forced-card probabilities.
 // Uniformly accepting feasible paths WITHOUT this weight would be biased.
 return {counts:left,draws:cards.length,player:score.player,banker:score.banker,
  result:outcome(score),weight,cards};
}
function pick(pool,mass,rng){
 let u=rng()*mass;
 for(const x of pool)if((u-=x.weight)<0)return x.counts;
 return pool[pool.length-1].counts;
}
function posterior(history,scores,opts={}){
 const observed=validateScores(history,scores),cfg=core.config(opts);
 // Preserve the exact old posterior for an entirely unknown score sequence.
 if(observed.every(s=>s===null))return core.posterior(history,cfg);
 const seed=core.seedFor(Array.from(JSON.stringify([OBSERVATION_MODEL,history,observed])));
 const rng=rngFrom(seed),n=cfg.PARTICLE_COUNT;
 let particles=Array.from({length:n},()=>BASE.slice()),weights=Array(n).fill(1/n);
 let ess=n,diversity=1,resampleCount=0,depthExpected=TOTAL;
 if(history.length>cfg.MAX_HANDS)return {available:false,reason:"shoe_depth_limit",config:cfg,seed};
 for(let hand=0;hand<history.length;hand++){
  const score=observed[hand],branches=score?branchesFor(score):null;
  const target=TOTAL-(hand+1)*cfg.AVERAGE_CARDS_PER_HAND,sigma=2.5+Math.sqrt(hand+1);
  const pools=new Array(n),masses=new Array(n).fill(0),evidence=new Array(n).fill(0);
  for(let i=0;i<n;i++){
   const pool=[];pools[i]=pool;
   if(weights[i]===0)continue;
   for(let j=0;j<cfg.LIKELIHOOD_DRAWS;j++){
    const trial=score?propose(particles[i],score,rng,branches):core.deal(particles[i],rng);
    if(!trial||(!score&&trial.result!==history[hand]))continue;
    const z=(sum(trial.counts)-target)/sigma;
    const weight=(score?trial.weight:1)*Math.exp(-.5*cfg.DEPTH_RELIABILITY*z*z);
    if(weight>0){pool.push({counts:trial.counts,weight});masses[i]+=weight;}
   }
   evidence[i]=weights[i]*masses[i]/cfg.LIKELIHOOD_DRAWS;
  }
  const mass=sum(evidence);
  if(!(mass>0))return {available:false,reason:"score_particle_collapse",config:cfg,seed};
  const w=evidence.map(x=>x/mass);ess=1/sum(w.map(x=>x*x));
  const next=new Array(n);
  // Remove zero-mass ancestors so every slot follows the full observed history;
  // stale dead states would also distort the core's unweighted depth diagnostic.
  if(ess/n<cfg.RESAMPLE_ESS_RATIO||w.some(x=>x===0)){
   const start=rng()/n,used=new Set();let parent=0,cumulative=w[0];
   for(let k=0;k<n;k++){
    const u=start+k/n;
    while(parent<n-1&&(u>=cumulative||w[parent]===0))cumulative+=w[++parent];
    used.add(parent);
    // Draw separately from the parent's weighted legal pool for each child.
    // No arbitrary card mutation and no B/P/T-only fallback for known totals.
    next[k]=pick(pools[parent],masses[parent],rng).slice();
   }
   weights=Array(n).fill(1/n);diversity=used.size/n;resampleCount++;
  }else{
   for(let i=0;i<n;i++)next[i]=w[i]>0?pick(pools[i],masses[i],rng).slice():particles[i];
   weights=w;diversity=w.filter(x=>x>0).length/n;
  }
  particles=next;depthExpected=target;
 }
 return {available:true,particles,weights,ess,diversity,resampleCount,depthExpected,
  config:cfg,seed,observed_actual_cards:false,soft_depth_constraint:true};
}
function run(history,scores,opts={},externalModel=null,sequenceRuntime=null,sequenceModel=null){
 const checked=validateScores(history,scores),known=checked.filter(x=>x!==null).length;
 if(!known)return core.run(history,opts,externalModel,sequenceRuntime,sequenceModel);
 const report=core.run(history,opts,externalModel,sequenceRuntime,sequenceModel,
  (h,cfg)=>posterior(h,checked,cfg));
 return {...report,version:VERSION,observation_model:OBSERVATION_MODEL,
  observation_summary:{total_hands:history.length,score_hands:known,bpt_only_hands:history.length-known},
  observed_actual_cards:false};
}
module.exports=Object.freeze({VERSION,OBSERVATION_MODEL,validateScores,branchesFor,propose,posterior,run});
