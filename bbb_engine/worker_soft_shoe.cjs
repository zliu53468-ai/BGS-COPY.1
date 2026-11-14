"use strict";
/* COPY transport: original B/P/T or explicit final-total observations. */
const readline=require("node:readline");
global.self=global;
const roadAux=require("./derived_road_aux.js");
const core=require("./soft_shoe_decision_runtime.js");
const scoreCore=require("./soft_shoe_score_observation.cjs");
require("./soft_shoe_model_bundle.js");
const frozen=require("./soft_shoe_frozen_lstm_runtime.js");
require("./soft_shoe_frozen_model_bundle.js");
const SOURCE_COMMIT="ce34c8be77df4fc5922a59454daf3dc22d1a1898";
const CORE_BLOB="4c24da8aebb51ffce0826352ea372afdf09a9c71";
const OBSERVATION_BLOB="59cab331bcae0cca7be7aaa0aaec04d673ec11c6";
// COPY-specific sampling profile; forecasting rules stay behind the core hook.
const COPY_PARAMETERS=Object.freeze({PARTICLE_COUNT:512,LIKELIHOOD_DRAWS:96,NEXT_HAND_DRAWS:192});
function evaluate(input){
 if(!input||!Array.isArray(input.history)||
    input.history.some(x=>!["B","P","T"].includes(x)))
  throw Error("invalid_bpt_history");
 // Same history + scores + profile gives the same result; never seed from LINE UID.
 const scores=Object.hasOwn(input,"scores")?scoreCore.validateScores(input.history,input.scores):null;
 const usesScores=scores!==null&&scores.some(x=>x!==null);
 const report=usesScores?
  scoreCore.run(input.history,scores,COPY_PARAMETERS,global.BBB_SOFT_SHOE_XGB_MODEL,
   frozen,global.BBB_SOFT_SHOE_LSTM_MODEL):
  core.run(input.history,COPY_PARAMETERS,global.BBB_SOFT_SHOE_XGB_MODEL,
   frozen,global.BBB_SOFT_SHOE_LSTM_MODEL);
 if(report.version!==(usesScores?scoreCore.VERSION:core.VERSION))throw Error("unexpected_bbb_core_version");
 if(!roadAux||typeof roadAux.analyze!=="function"||
    report.config?.ROAD_AUX_ENABLED!==true||
    report.config?.ROAD_AUX_ADAPTIVE_ENABLED!==true||
    !report.road_aux||report.road_aux.estimated_only!==true||
    report.road_aux.validated_betting_edge!==false)
  throw Error("bbb_road_aux_contract_changed");
 if(report.action!=="Skip"||report.validated_edge!==false||
    report.bankroll_fraction!==0)throw Error("unverified_betting_gate_changed");
 return {source_commit:SOURCE_COMMIT,source_blob:CORE_BLOB,observation_source_blob:OBSERVATION_BLOB,report};
}
const rl=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
rl.on("line",line=>{
 let response;
 try{response={ok:true,result:evaluate(JSON.parse(line))};}
 catch(e){response={ok:false,error:String(e?.message||e).slice(0,250)};}
 process.stdout.write(JSON.stringify(response)+"\n");
});
module.exports={evaluate,SOURCE_COMMIT,CORE_BLOB,COPY_PARAMETERS};
