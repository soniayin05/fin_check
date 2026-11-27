"""Same cases with planned recovery enabled/disabled; gold stays in the scorer."""

from .evaluation import load_cases, run_comparison
from .faults import FaultInjector
from .workflow import Workflow


class FaultSystem:
    def __init__(self,facts,passages,model,cases,planned,max_calls,max_turns):
        self.facts,self.passages,self.model=facts,passages,model
        self.config={c['claim']:c['fault'] for c in cases}
        self.planned,self.max_calls,self.max_turns=planned,max_calls,max_turns

    def run(self,claim):
        config=self.config[claim]
        injector=FaultInjector(config['kind'],config.get('variant','unit'),config.get('target','calculate'))
        return Workflow(self.facts,self.passages,self.model,self.max_calls,self.max_turns,
                        require_plan=self.planned,fault=injector).run(claim)


def fault_command(args,parser,root,facts,passages):
    from .model import Gemini, ModelError
    try:
        cases=load_cases(args.cases)
        if args.only:cases=[c for c in cases if c['id'] in args.only.split(',')]
        if not cases:raise ValueError('No matching fault cases')
        for c in cases:
            config=c.get('fault',{})
            FaultInjector(config.get('kind'),config.get('variant','unit'),config.get('target','calculate'))
        if len({c['claim'] for c in cases})!=len(cases):raise ValueError('Fault claims must be distinct')
        if args.dry_run:
            print({'cases':len(cases),'scheduled_runs':2*len(cases),'model_calls_made':0});return 0
        model=Gemini()
        systems={name:FaultSystem(facts,passages,model,cases,planned,args.max_calls,args.max_turns)
                 for name,planned in [('planned_recovery',True),('no_explicit_plan',False)]}
        target,summary=run_comparison(root,cases,systems,args.facts,args.cases,
            dict(model=model.model,max_calls=args.max_calls,max_turns=args.max_turns,
                 experiment='planning_plus_recovery_vs_disabled',retriever=passages.default_method))
        print(summary);print(f'Fault results: {target}')
        return 0
    except (ValueError,KeyError,OSError,ModelError) as error:parser.error(str(error))
