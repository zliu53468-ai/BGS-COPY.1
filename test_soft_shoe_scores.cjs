"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),crypto=require("node:crypto");
const path=require("node:path"),Module=require("node:module");
global.self=global;
const core=require("./bbb_engine/soft_shoe_decision_runtime.js");
const scores=require("./bbb_engine/soft_shoe_score_observation.cjs");
require("./bbb_engine/soft_shoe_model_bundle.js");
const frozen=require("./bbb_engine/soft_shoe_frozen_lstm_runtime.js");
require("./bbb_engine/soft_shoe_frozen_model_bundle.js");
const base=[128,32,32,32,32,32,32,32,32,32];
const sum=a=>a.reduce((x,y)=>x+y,0);
const near=(a,b)=>assert.ok(Math.abs(a-b)<1e-11,`${a} != ${b}`);
function rngFrom(seed){let x=seed;return()=>{x^=x<<13;x^=x>>>17;x^=x<<5;return(x>>>0)/4294967296;};}
function replay(counts,cards){
 let left=counts.slice(),index=0;
 const trial=core.deal(counts,()=>{
  const rank=cards[index++];assert.ok(Number.isInteger(rank));assert.ok(left[rank]>0);
  const u=(sum(left.slice(0,rank))+.5*left[rank])/sum(left);left[rank]--;return u;
 });
 assert.equal(index,cards.length);return trial;
}
// Every proposed path must be a real no-replacement core deal, including
// natural termination and all of the player's/banker's third-card branches.
for(let player=0;player<10;player++)for(let banker=0;banker<10;banker++){
 const score={player,banker},rng=rngFrom(1234+player*10+banker);
 for(let j=0;j<24;j++){
  const q=scores.propose(base,score,rng);assert.ok(q);
  const replayed=replay(base,q.cards);
  assert.equal(replayed.player,player);assert.equal(replayed.banker,banker);
  assert.deepEqual(q.counts,replayed.counts);assert.equal(sum(q.counts),416-q.draws);
  assert.ok(q.weight>0&&q.counts.every(x=>Number.isInteger(x)&&x>=0));
 }
}
// Independent exact finite-shoe oracle: enumerate all ordered physical
// six-card samples, let the unchanged dealer consume 4/5/6, marginalize extras.
function exactLaw(counts){
 const result=new Map();
 function visit(left,cards,prob){
  if(cards.length===6){
   let i=0,remaining=counts.slice();
   const trial=core.deal(counts,()=>{
    const r=cards[i++],u=(sum(remaining.slice(0,r))+.5*remaining[r])/sum(remaining);
    remaining[r]--;return u;
   });
   const key=trial.player*10+trial.banker;
   if(!result.has(key))result.set(key,{mass:0,counts:Array(10).fill(0)});
   const row=result.get(key);row.mass+=prob;
   for(let r=0;r<10;r++)row.counts[r]+=prob*trial.counts[r];return;
  }
  const total=sum(left);
  for(let r=0;r<10;r++)if(left[r]){
   const n=left[r];left[r]--;visit(left,cards.concat(r),prob*n/total);left[r]++;
  }
 }
 visit(counts.slice(),[],1);return result;
}
for(const counts of [[2,1,1,0,1,1,1,1,0,0],[1,0,0,1,1,0,1,0,2,2]]){
 const exact=exactLaw(counts);near(sum([...exact.values()].map(x=>x.mass)),1);
 for(let player=0;player<10;player++)for(let banker=0;banker<10;banker++){
  const score={player,banker},branches=scores.branchesFor(score),total=sum(counts);
  let mass=0,expected=Array(10).fill(0);
  // Exhaust q's entire support: branch, P1, B1. Importance correction must
  // recover both likelihood AND the remaining-count first moments exactly.
  for(let k=0;k<branches.length;k++)for(let p1=0;p1<10;p1++)if(counts[p1]){
   const left=counts.slice();left[p1]--;
   for(let b1=0;b1<10;b1++)if(left[b1]){
    const numbers=[(k+.5)/branches.length,
     (sum(counts.slice(0,p1))+.5*counts[p1])/total,
     (sum(left.slice(0,b1))+.5*left[b1])/(total-1)];
    const proposal=scores.propose(counts,score,()=>numbers.shift(),branches);
    if(!proposal)continue;
    const q=counts[p1]/total*left[b1]/(total-1)/branches.length;
    const weight=q*proposal.weight;mass+=weight;
    for(let r=0;r<10;r++)expected[r]+=weight*proposal.counts[r];
   }
  }
  const wanted=exact.get(player*10+banker)||{mass:0,counts:Array(10).fill(0)};
  near(mass,wanted.mass);for(let r=0;r<10;r++)near(expected[r],wanted.counts[r]);
 }
}
// Original core reconstructed only by removing the documented extension.
// Its git hash must match the pinned upstream blob; compare full B/P/T reports.
const file=path.resolve("bbb_engine/soft_shoe_decision_runtime.js");
const current=fs.readFileSync(file,"utf8");
const original=current.replace(
 "function run(history,opts={},externalModel=null,sequenceRuntime=null,sequenceModel=null,observationPosterior=null){",
 "function run(history,opts={},externalModel=null,sequenceRuntime=null,sequenceModel=null){").replace(
 '  // COPY observation extension: forecasting/EV/gates below are unchanged.\n'+
 '  if(observationPosterior!==null&&typeof observationPosterior!=="function")throw Error("invalid_observation_posterior");\n'+
 '  const cfg=config(opts),post=observationPosterior===null?\n'+
 '   posterior(history,cfg):observationPosterior(history,cfg);\n'+
 '  const seed=post.seed;',
 '  const cfg=config(opts),seed=seedFor(history),post=posterior(history,cfg,seed);');
const hash=crypto.createHash("sha1").update(`blob ${Buffer.byteLength(original)}\0`).update(original).digest("hex");
assert.equal(hash,"13ee5a6f829d451b4c156935a9401680a705311b");
const old=new Module(file,module);old.filename=file;old.paths=module.paths;old._compile(original,file);
const cfg={PARTICLE_COUNT:512,LIKELIHOOD_DRAWS:96,NEXT_HAND_DRAWS:192};
const args=[cfg,global.BBB_SOFT_SHOE_XGB_MODEL,frozen,global.BBB_SOFT_SHOE_LSTM_MODEL];
for(const h of [[],Array.from("PBT"),Array.from("BPPBTBBPPTBP")]){
 const expected=old.exports.run(h,...args);
 assert.deepEqual(core.run(h,...args),expected);
 assert.deepEqual(scores.run(h,h.map(()=>null),...args),expected);
}
// Scores must update the posterior, not just the random seed: equal RNG seed
// is not needed for this invariant. Final 9/9 permits both naturals and
// drawn hands; final 0/0 requires six cards. Never infer naturals from totals.
const natural=scores.posterior(["T"],[{player:9,banker:9}],cfg);
assert.ok(natural.available);assert.deepEqual(new Set(natural.particles.map(sum)),new Set([410,412]));
assert.ok(natural.particles.every(p=>p[0]<128||p.slice(1).some((x,r)=>x<32)));
const zero=scores.posterior(["T"],[{player:0,banker:0}],cfg);
assert.ok(zero.available);assert.ok(zero.particles.every(p=>sum(p)===410));
const mixed=scores.posterior(["P","B","T"],[{player:8,banker:4},null,{player:9,banker:9}],cfg);
assert.ok(mixed.available);near(sum(mixed.weights),1);
assert.ok(mixed.particles.every(p=>sum(p)>=398&&sum(p)<=404));
assert.deepEqual(mixed,scores.posterior(["P","B","T"],[{player:8,banker:4},null,{player:9,banker:9}],cfg));
assert.throws(()=>scores.validateScores(["P"],[{player:2,banker:8}]),/mismatch/);
assert.throws(()=>scores.validateScores(["P"],[]),/length/);
assert.throws(()=>scores.validateScores(["P"],[{player:true,banker:0}]),/invalid/);
assert.equal(scores.posterior(Array(73).fill("T"),Array(73).fill({player:9,banker:9}),cfg).reason,"shoe_depth_limit");
// Every 0/0 consumes exactly six cards. Near depletion, infeasible parents
// must disappear rather than retaining an old shoe in zero-weight slots.
const zeros68=Array(68).fill({player:0,banker:0}),ties68=Array(68).fill("T");
const depleted=scores.posterior(ties68,zeros68,cfg);
assert.equal(depleted.available,true);near(sum(depleted.weights),1);
assert.ok(depleted.weights.every(x=>x>0));
assert.ok(depleted.particles.every(p=>sum(p)===8&&p.every(x=>Number.isInteger(x)&&x>=0)));
// Reuse the computed posterior through the production hook to verify that
// diagnostics/next-hand simulation see only legal states, without a rerun.
const depletedReport=core.run(ties68,...args,()=>depleted);
assert.equal(depletedReport.estimated_remaining_cards,8);
for(const [hands,reason] of [[69,"physical_simulation_unavailable"],[70,"score_particle_collapse"]]){
 const report=scores.run(Array(hands).fill("T"),Array(hands).fill({player:0,banker:0}),...args);
 assert.equal(report.reason,reason);assert.equal(report.action,"Skip");
 assert.equal(report.validated_edge,false);assert.equal(report.bankroll_fraction,0);
 assert.equal(report.observation_summary.score_hands,hands);
 assert.equal(report.probabilities,undefined);
}
console.log("Score observation: legal paths, exact finite-shoe weights/counts, BPT parity and posterior invariants PASS");
