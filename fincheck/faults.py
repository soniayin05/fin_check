"""Bounded fault injection for development; never fabricates real evidence."""

from copy import deepcopy


class FaultInjector:
    def __init__(self,kind,variant='unit',target='calculate'):
        if kind not in {'failure','timeout','bad_data'}:raise ValueError('Unknown fault kind')
        if variant not in {'unit','period','duplicate','value','source'}:raise ValueError('Unknown bad-data variant')
        self.kind,self.variant,self.target=kind,variant,target
        self.injected=False;self.events=[]

    def __call__(self,name,args,execute):
        if name!=self.target or self.injected:return execute()
        self.injected=True
        self.events.append(dict(kind='fault',scope='evidence',name=name,fault=self.kind,
                                variant=self.variant,injected=True,point='before_tool_return'))
        if self.kind=='failure':raise ConnectionError('Injected transient read failure')
        if self.kind=='timeout':raise TimeoutError('Injected tool timeout')
        result=deepcopy(execute())
        rows=result.get('observations',[])
        if self.variant=='unit' and rows:rows[0]['unit']='invalid_unit'
        elif self.variant=='period' and rows:rows[0]['period_end']='1900-01-01'
        elif self.variant=='duplicate' and rows:rows.append(deepcopy(rows[0]))
        elif self.variant=='source':result['sources']={'injected-source':{'url':'invalid'}}
        else:result['value']=float(result.get('value',0))+123
        return result
