#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@POD based reduced order model
Used for solving the reduced-order equation generated by POD basis method
including POD-G   :: solve the equation with online Newton-like iteration method
          POD-NN  :: solve the equation with offline  NN trained only with sample points 
          POD-PINN:: solve the euuation with offline  NN trained with sample points and equation
For general use, the reduced equations are simplified as the following formulation:
          alpha' * A * alpha + B * alpha = f
          where A B C and f are all functions of design parameters
          
@ Customed problem
2D Lid Driven cavity Problem:
    >  u_xp + v_yp = 0
    >  u*u_xp + v*u_yp = -p_xp + mu*(u2_xp2 + u2_yp2) = 0
    >  u*v_xp + v*v_yp = -p_yp + mu*(v2_xp2 + v2_yp2) = 0
    > where mu = 1/Re
    - transformation between computational space and physical space
    - computational-to-physical Transformation
    - xp = xc*xCoef       + yc*yCoef*cosTh
    - yp = yc*yCoef*sinTh
    - computational-to-local Transformation
    - xc = (xp - yp*cotTh)/xCoef
    - yc = yp/yCoef/sinTh
    - Jac = [ d(xc, yc)/d(xp, yp) ]^T
    -     = [ 1/xCoef   ,              0
    -        -cotTh/xCoef, 1/yCoef/sinTh        ]
    > alpha = (Re, TH) in [100, 500]x[pi/6,5pi/6] is design parameters
Reduced order equations:
    > p_Modes' * Eq1 + u_Modes' * Eq2 +v_Modes * Eq3 = 0
    


Created on Wed Mar 18 14:40:38 2020

@author: wenqianchen
"""



import sys
sys.path.insert(0,'../tools')
sys.path.insert(0,'../tools/NNs')

from Chebyshev import Chebyshev2D
from scipy.io import loadmat
import numpy as np
import torch
from NN import POD_Net, DEVICE
from Normalization import Normalization
from scipy.optimize import fsolve

# Eqs parameters
NVAR = 3     # the number of unknown variables: p,u,v
NVARLOAD = 6 # the number of loaded variables: p,u,v,t(dummy),omega,psi
Newton = {'iterMax':100, 'eps':1E-6}

# reproducible
torch.manual_seed(1234)  
np.random.seed(1234)


class CustomedEqs():    
    def __init__(self, matfilePOD,PODNum,matfileValidation, M):
        datas = loadmat(matfilePOD)
        # data for POD
        #PODNum=3
        self.Samples      = datas['Samples'][:,0:PODNum]
        self.FieldShape   = tuple(datas['FieldShape'][0])
        self.parameters   = datas['parameters'][0:PODNum,:]
        self.design_space = datas['design_space']
        self.NSample = self.Samples.shape[1]
        
        # data for validation
        datas = loadmat(matfileValidation)
        self.ValidationParameters   = datas['parameters']
        self.ValidationSamples      = self.ExtractInteriorSnapshots( datas['Samples'] )
    
        
        # svd decomposition
        self.Modes, self.sigma, _ = np.linalg.svd( self.ExtractInteriorSnapshots(self.Samples) );
        self.Modes = self.Modes[:,:M]
        self.M = M
        
        # spatial discretization
        self.Chby2D   = Chebyshev2D(xL=-1, xR=1, yD=-1, yU=1, Mx=self.FieldShape[0]-1,My=self.FieldShape[1]-1)
        self.dxp,self.dyp  = self.Chby2D.DxCoeffN2()
        self.dx, self.dy   = self.Chby2D.DxCoeff(1) 
        self.d2x, self.d2y = self.Chby2D.DxCoeff(2)

        # projections
        self.projections = np.matmul( self.Modes.T, self.ExtractInteriorSnapshots(self.Samples))
        _, Mapping  = Normalization.Mapstatic(self.projections.T)
        self.proj_mean =  Mapping[0][None,:] 
        self.proj_std  =  Mapping[1][None,:] 
        
        
        # reduced-order equations
        self.Interior = np.zeros(self.FieldShape)
        self.Interior[1:-1,1:-1]=1
        self.Boundary=1-self.Interior
        self.uBC = np.reshape(self.Samples[1::NVARLOAD,0], self.FieldShape)*self.Boundary
        self.InteriorShape = (self.FieldShape[0]-2, self.FieldShape[1]-2,)
        self.Beqs, self.Bbc = self.getB()
        self.Aeqs, self.Abc = self.getA()
        
        # Compute projection error
        self.lamda_proj = np.matmul(self.ValidationSamples.T, self.Modes)
        self.ProjError = self.GetError(self.lamda_proj)
        
    def Mode2Field(self, Vec):
        p,u,v = np.zeros(self.FieldShape), np.zeros(self.FieldShape), np.zeros(self.FieldShape)
        p[1:-1,1:-1] = np.reshape( Vec[0::NVAR], self.InteriorShape)
        u[1:-1,1:-1] = np.reshape( Vec[1::NVAR], self.InteriorShape)
        v[1:-1,1:-1] = np.reshape( Vec[2::NVAR], self.InteriorShape)
        return p,u,v
    def ExtractInteriorSnapshots(self,Samples):
        NSample = Samples.shape[1]
        Samples_shape = (self.FieldShape[0], self.FieldShape[1],NVARLOAD,NSample,)
        return np.reshape( np.reshape(Samples, Samples_shape)[1:-1, 1:-1, 0:NVAR, :], (-1, NSample))
    
    def Compute_d_dxc(self, phi):
        return np.matmul(self.dx,phi)
    def Compute_d_dyc(self, phi):
        return np.matmul(self.dy, phi.T).T
    def Compute_dp_dxc(self, phi):
        return np.matmul(self.dxp,phi)
    def Compute_dp_dyc(self, phi):
        return np.matmul(self.dyp, phi.T).T    
    def Compute_d_dxc2(self, phi):
        return self.Compute_d_dxc( self.Compute_d_dxc(phi) )
    def Compute_d_dyc2(self, phi):
        return self.Compute_d_dyc( self.Compute_d_dyc(phi) )
    def Compute_d_dxcyc(self, phi):
        return self.Compute_d_dyc( self.Compute_d_dxc(phi) )
    def Compute_d_d1(self, phi):
        return self.Compute_d_dxc(phi), self.Compute_d_dyc(phi)
    def Compute_d_d1p(self, phi):
        return self.Compute_dp_dxc(phi), self.Compute_dp_dyc(phi)
    def Compute_d_d2(self, phi):
        return self.Compute_d_dxc2(phi), self.Compute_d_dyc2(phi), self.Compute_d_dxcyc(phi)
        
    # get A from the first mth modes
    def getA(self): 
        """0:3 namely first index is related to terms:
           index [      0           1            2             3    ]
           terms [uu_xc+uv_xc, uu_yc+uv_yc, vu_xc+vv_xc, vu_yc+vv_yc]
           weight[     J11   ,      J12   ,     J21    ,     J22    ]
           coeff [    u+v    ,      u+v   ,     u+v    ,     u+v    ]
        """
        Aeqs = np.zeros((4, self.M, self.M, self.M))
        Abc  = np.zeros((4, self.M, self.M))
        uBCxc, uBCyc= self.Compute_d_d1(self.uBC)
        for j in range(self.M):
            pj, uj, vj= self.Mode2Field(self.Modes[:,j])
            ujxc, ujyc= self.Compute_d_d1(uj)      
            vjxc, vjyc= self.Compute_d_d1(vj)  
            for k in range(self.M):
                pk, uk,vk = self.Mode2Field(self.Modes[:,k])
                for i in range(self.M):
                    pi, ui,vi = self.Mode2Field(self.Modes[:,i])
                    Aeqs[0,k,i,j] = ( self.Interior*(ui*ujxc*uk + ui*vjxc*vk) ).sum()
                    Aeqs[1,k,i,j] = ( self.Interior*(ui*ujyc*uk + ui*vjyc*vk) ).sum()
                    Aeqs[2,k,i,j] = ( self.Interior*(vi*ujxc*uk + vi*vjxc*vk) ).sum()
                    Aeqs[3,k,i,j] = ( self.Interior*(vi*ujyc*uk + vi*vjyc*vk) ).sum()
                    Abc[0,k,i]  = ( self.Interior*(ui*uBCxc*uk              ) ).sum()
                    Abc[1,k,i]  = ( self.Interior*(ui*uBCyc*uk              ) ).sum()
                    Abc[2,k,i]  = ( self.Interior*(vi*uBCxc*uk              ) ).sum()
                    Abc[3,k,i]  = ( self.Interior*(vi*uBCyc*uk              ) ).sum()
        return Aeqs,Abc
        
    def getB(self):
        """0:10 namely first index is related to terms:    
           index  [    0          1          2          3               4               5                    6     ]
           terms  [u_xc+p_xc, u_yc+p_yc, v_xc+p_xc, v_yc+p_yc,    -u_xc2-v_xc2 , -u_xcyc-v_xcyc   ,   -u_yc2-v_yc2 ]
           weight [   p+u   ,    p+u   ,    p+v   ,    p+v   ,        u+v      ,       u+v        ,        u+v     ]
           coeff  [   J11   ,    J12   ,    J21   ,    J22   ,  v(J11^2+J21^2) ,2v(J11J12+J21J22) ,  v(J12^2+J22^2)]
        """
        
        Beqs = np.zeros((7,self.M, self.M))
        Bbc  = np.zeros((7,self.M))
        
        uBCxc, uBCyc= self.Compute_d_d1(self.uBC)
        uBCxc2, uBCyc2, uBCxcyc = self.Compute_d_d2(self.uBC)       
        for j in range(self.M):
            pj, uj, vj= self.Mode2Field(self.Modes[:,j])      
            ujxc, ujyc= self.Compute_d_d1(uj)
            vjxc, vjyc= self.Compute_d_d1(vj)
            pjxc, pjyc= self.Compute_d_d1p(pj)
            ujxc2, ujyc2, ujxcyc = self.Compute_d_d2(uj)
            vjxc2, vjyc2, vjxcyc = self.Compute_d_d2(vj)
            for i in range(self.M):
                pi, ui,vi = self.Mode2Field(self.Modes[:,i])
                Beqs[0,i,j] = ( self.Interior*(  ujxc*pi + pjxc*ui ) ).sum()
                Beqs[1,i,j] = ( self.Interior*(  ujyc*pi + pjyc*ui ) ).sum()
                Beqs[2,i,j] = ( self.Interior*(  vjxc*pi + pjxc*vi ) ).sum()
                Beqs[3,i,j] = ( self.Interior*(  vjyc*pi + pjyc*vi ) ).sum()
                Beqs[4,i,j] =-( self.Interior*( ujxc2*ui + vjxc2*vi) ).sum()
                Beqs[5,i,j] =-( self.Interior*( ujyc2*ui + vjyc2*vi) ).sum()
                Beqs[6,i,j] =-( self.Interior*(ujxcyc*ui +vjxcyc*vi) ).sum()

                Bbc[0,i] = ( self.Interior*( uBCxc*pi              ) ).sum()
                Bbc[1,i] = ( self.Interior*( uBCyc*pi              ) ).sum()
                Bbc[2,i] = ( self.Interior*( 0*pi                  ) ).sum()
                Bbc[3,i] = ( self.Interior*( 0*pi                  ) ).sum()
                Bbc[4,i] =-( self.Interior*( uBCxc2*ui             ) ).sum()
                Bbc[5,i] =-( self.Interior*( uBCyc2*ui             ) ).sum()
                Bbc[6,i] =-( self.Interior*(uBCxcyc*ui             ) ).sum()                
        return Beqs,Bbc
    def getJac(self,alpha, cos=np.cos, sin=np.sin, cat=np.concatenate):
        Theta = alpha[:,1:2]/180*3.14159265359        
        xCoef, yCoef = 1/2, 1/2
        Jac11=1/xCoef*(Theta*0+1)
        Jac12=0.0*Theta
        Jac21=-cos(Theta)/sin(Theta)/xCoef
        Jac22= 1/yCoef/sin(Theta)
        return Jac11,Jac12,Jac21,Jac22
    def getGrid(self,alpha,cos=np.cos, sin=np.sin, cat=np.concatenate):
        xc,yc = self.Chby2D.grid()
        xCoef, yCoef = 1/2, 1/2
        Theta = alpha[:,1:2]/180*3.14159265359 
        xp = xc*xCoef + yc*yCoef*cos(Theta)
        yp = yc*yCoef*sin(Theta)
        return xp,yp
    def getABCoef(self, alpha, cos=np.cos, sin=np.sin, cat=np.concatenate ):
        v    = 1/alpha[:,0:1]
        Jac11,Jac12,Jac21,Jac22 = self.getJac(alpha, cos=cos, sin=sin, cat=cat)
        Acoef = cat((Jac11, Jac12, Jac21, Jac22),axis=1)
        BCoef = cat((Jac11, Jac12, Jac21, Jac22, v*(Jac11**2+Jac21**2), v*(Jac12**2+Jac22**2), 2*v*(Jac11*Jac12+Jac21*Jac22)), axis=1)
        return Acoef, BCoef
    
    def POD_Gfsolve(self,alpha, lamda_init=None):
        n = alpha.shape[0]
        lamda  = np.zeros((n, self.M))
        def compute_eAe(A, e):
            tmp  = np.matmul(e.T, A)
            return np.matmul(tmp, e).squeeze(axis=(2))
        def eqs(x,A,B,source):
            lamda = x[:,None];
            lamda = lamda*self.proj_std.T + self.proj_mean.T
            err = compute_eAe(A,lamda) + np.matmul(B,lamda) -source
            return err.squeeze()
        for i in range(n):
            alphai = alpha[i:i+1,0:2]
            AiCoeff, BiCoeff = self.getABCoef(alphai)
            AiCoeff, BiCoeff = AiCoeff.squeeze(axis=0), BiCoeff.squeeze(axis=0)
            Ai = ( AiCoeff[:,None,None,None]* self.Aeqs ).sum(axis=0)
            Bi = ( AiCoeff[:,None,None]* self.Abc  ).sum(axis=0) \
                +( BiCoeff[:,None,None]* self.Beqs ).sum(axis=0)
            sourcei = -( BiCoeff[:,None]* self.Bbc  ).sum(axis=0)[:,None]
            
            if lamda_init is None:
                dis = (alphai - self.parameters)/ (self.design_space[1:2,:]-self.design_space[0:1,:] )
                dis = np.linalg.norm(dis, axis=1);
                ind = np.where(dis == dis.min())[0][0]
                lamda0 = self.projections[0:self.M, ind:ind+1].T
            else:
                lamda0 = lamda_init[i:i+1,:]   
            lamda0 = (lamda0-self.proj_mean)/self.proj_std
            lamdasol = fsolve(lambda x: eqs(x,Ai,Bi,sourcei), lamda0.squeeze())
            err = np.linalg.norm( eqs(lamdasol, Ai, Bi, sourcei) )
            if err > Newton["eps"]:
                print('Case %d: (%f,%f) can only reach to an error of %f'%(i, alphai[0,0], alphai[0,1], err))
                #lamdasol = lamdasol*0 + np.inf
            lamda[i,:] = lamdasol[None,:]*self.proj_std + self.proj_mean
            
        return lamda
    
    
    def GetError(self,lamda):
        Nvalidation =self.ValidationParameters.shape[0]
        if  Nvalidation != lamda.shape[0]:
            raise Exception('The number of lamda should be equal to validation parameters')
        phi_pred         = np.matmul( lamda, self.Modes.T)
        phi_Num          = self.ValidationSamples.T
        Error = np.zeros((Nvalidation,NVAR))    # the second dimension is [p,u,v]
        for nvar in range(NVAR):
            Error[:,nvar] = np.linalg.norm(phi_Num[:,nvar::NVAR]-phi_pred[:,nvar::NVAR], axis = 1)\
                           /np.linalg.norm(phi_Num[:,nvar::NVAR], axis=1)
        Errorpuv = Error.mean(axis=0)
        Errortotal =  np.linalg.norm(phi_Num[:,:]-phi_pred[:,:], axis = 1)\
                                   /np.linalg.norm(phi_Num[:,:], axis=1)
        print(Errortotal)
        Errortotal = Errortotal.mean(axis=0)
        print("Errors=[%f,%f,%f],%f"%(Errorpuv[0],Errorpuv[1],Errorpuv[2], Errortotal))
        return Errorpuv, Errortotal
        
    def GetPredFields(self,alpha,lamda, filename):
        Ncase = lamda.shape[0]
        Fields = []
        phi_pred  = np.matmul( lamda, self.Modes.T)
        J11,J12,J21,J22 = self.getJac(alpha)
        for icase in range(Ncase):
            alphai = alpha[icase:icase+1,:]
            pi,ui,vi = self.Mode2Field(phi_pred[icase,:])
            ui = ui +self.uBC
            ## compute vorticity and streamfunction
            # u_yp - v_xp = u_xc*J21+u_yc*J22 -v_xc*J11-v_yc*J12
            ui_xc, ui_yc= self.Compute_d_d1(ui)
            vi_xc, vi_yc= self.Compute_d_d1(vi)
            xp,yp = self.getGrid(alphai)
            hx = abs(xp[0,0]-xp[1,0])
            hy = abs(yp[0,0]-yp[0,1])
            omegai =  ui_xc*J21[icase]+ui_yc*J22[icase] -vi_xc*J11[icase]-vi_yc*J12[icase]
            ## solve psi with explicit method
            # psi_xp2 + psi_yp2 = psi_xc2*(J11^2+J21^2)+psi_yc2*(J12^2+J22^2)+psi_xcyc*(2*J11*J12+2*J21*J22)
            #                   = omega
            dt = 0.5*min(hx,hy)**2
            psii = 0*omegai
            for it in range(int(1E8)):
                psi_xc2,psi_yc2, psi_xcyc = self.Compute_d_d2(psii) 
                dpsi = psi_xc2*(J11[icase]**2+J21[icase]**2)\
                      +psi_yc2*(J12[icase]**2+J22[icase]**2)\
                      +psi_xcyc*(2*J11[icase]*J12[icase]+2*J21[icase]*J22[icase])\
                      -omegai
                dpsi = dpsi * self.Interior
                psii = psii +dpsi*dt
                if it%10000==0:
                    print('%8d, dpsi=%e'%(it, np.abs(dpsi).max()))
                if np.abs(dpsi).max() < 1E-8:
                    break
            
            # write result
            Nx,Ny = self.FieldShape
            with open(filename+'%d'%icase+'.plt','w') as f:
                header = """
title="result"
variables="x","y","P","u","v","omega","psi"
zone,j=%d, i=%d,f=point"""%(Ny,Nx) + "\n"
                f.write(header)
                for j in range(Ny):
                    for i in range(Nx):
                        line=("%21.16f\t"*7 + "\n" )%(xp[i,j],yp[i,j],pi[i,j],ui[i,j],vi[i,j],omegai[i,j],psii[i,j])
                        f.write(line)
            
            Fields.append( np.stack((xp, yp, pi, ui, vi, omegai, psii), axis=0) )
        Fields = np.stack( tuple(Fields), axis=0)
        from scipy.io import savemat
        savemat(filename+'.mat', {'Fields':Fields})
        return Fields

    
class CustomedNet(POD_Net):
    def __init__(self, layers=None,oldnetfile=None,roeqs=None):
        super(CustomedNet, self).__init__(layers=layers,OldNetfile=oldnetfile)
        self.M = roeqs.M
        self.Aeqs = torch.tensor( roeqs.Aeqs ).float().to(DEVICE)
        self.Abc  = torch.tensor( roeqs.Abc  ).float().to(DEVICE)
        self.Beqs = torch.tensor( roeqs.Beqs ).float().to(DEVICE)
        self.Bbc  = torch.tensor( roeqs.Bbc  ).float().to(DEVICE)
        self.lb   = torch.tensor(roeqs.design_space[0:1,:]).float().to(DEVICE)
        self.ub   = torch.tensor(roeqs.design_space[1:2,:]).float().to(DEVICE)
        self.proj_std = torch.tensor( roeqs.proj_std ).float().to(DEVICE)
        self.proj_mean= torch.tensor( roeqs.proj_mean).float().to(DEVICE)
        self.roeqs = roeqs
        
        self.labeled_inputs  = torch.tensor( roeqs.parameters ).float().to(DEVICE)
        self.labeled_outputs = torch.tensor( roeqs.projections.T ).float().to(DEVICE)
        self.labeledLoss = self.loss_Eqs(self.labeled_inputs,self.labeled_outputs)
        pass
    def u_net(self,x):
        x = (x-(self.ub+self.lb)/2)/(self.ub-self.lb)*2
        out = self.unet(x)
        out = out*self.proj_std + self.proj_mean
        return out
    
    def forward(self,x):
        return self.u_net(x).detach().cpu().numpy()
    
    def loss_NN(self, xlabel, ylabel):
        y_pred    = self.u_net(xlabel)
        loss_NN   = self.lossfun(ylabel/self.proj_std, \
                                 y_pred/self.proj_std )
        return loss_NN
    
    def loss_PINN(self,x,dummy=None,weight=1):
        return self.loss_Eqs(x,self.u_net(x),weight)
        
    def loss_Eqs(self,x,lamda,weight=1):
        #lamda = self.u_net(x);
        ACoeff, BCoeff = self.roeqs.getABCoef(x, cos=torch.cos, sin=torch.sin, cat=torch.cat)
        A = ( ACoeff[:,:,None,None,None]* self.Aeqs[None,:,:,:,:] ).sum(axis=1)
        B = ( ACoeff[:,:,None,None]* self.Abc[ None,:,:,:]  ).sum(axis=1) \
           +( BCoeff[:,:,None,None]* self.Beqs[None,:,:,:]  ).sum(axis=1)
        source = -( BCoeff[:,:,None]* self.Bbc[None,:,:]  ).sum(axis=1)
        fx   = torch.matmul(lamda[:,None,None,:], A)
        fx   = torch.matmul(fx,lamda[:,None,:,None])
        fx   = fx.view(lamda.shape) + torch.matmul(B,lamda[:,:,None]).view(lamda.shape) -source
        return self.lossfun(weight*fx,torch.zeros_like(fx))
    

        
        
        
if __name__ == '__main__':
    NumSolsdir = 'NumSols/100_500and60_120'
    matfilePOD = NumSolsdir  + '/'+'LidDrivenPOD.mat'
    matfileValidation = NumSolsdir  + '/'+'LidDrivenValidation.mat'
    Nsample = 100
    M = 10
    roeqs = CustomedEqs(matfilePOD, Nsample, matfileValidation, M)


    from plotting import newfig,savefig
    import matplotlib.pyplot as plt    
    newfig(width=0.8)
    plt.semilogy(np.arange(roeqs.sigma.shape[0])+1, roeqs.sigma,'-ko')
    plt.xlabel('$m$')
    plt.ylabel('Singular value')    
    plt.show()
    savefig('fig/SingularValues_%d'%(Nsample) )
    plt.savefig('fig/SingularValues_%d_%dand%d_%d.png'%(roeqs.design_space[0,0],roeqs.design_space[1,0], \
                                                        roeqs.design_space[0,1],roeqs.design_space[1,1]) )
##    
#    
    
#    Net = CustomedNet(roeqs=roeqs,layers=[2,20,20,20,M])    
#    ind = [7,28,86]
#    roeqs.GetPredFields(roeqs.ValidationParameters[ind,:],roeqs.lamda_proj[ind,:], 'hh') 
#    alpha = roeqs.ValidationParameters
#    lamda = roeqs.POD_Gfsolve(alpha, roeqs.lamda_proj)
#    Errorpuv, Errortotal = roeqs.GetError(lamda)
#    print("Error=",Errorpuv, Errortotal)
    
#    Net = CustomedNet(roeqs=roeqs,layers=[2,20,20,20,M])
#    print(Net.labeledLoss)
#    Resi_inputs  = roeqs.ValidationParameters
#    dummy = np.zeros((Resi_inputs.shape[0],roeqs.M))
#    data = (Resi_inputs, dummy, 'Resi',0.9,)
#    from NN import train, train_options_default
#    options = train_options_default.copy()
#    options['weight_decay']=0
#    options['NBATCH'] = 10
#    train(Net,data,'tmp.net',options=options)
    
