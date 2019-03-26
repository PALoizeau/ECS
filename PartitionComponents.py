from Statemachine import Statemachine
import csv
from _thread import start_new_thread
import zmq
import logging
from ECSCodes import ECSCodes
codes = ECSCodes()
from states import CommonStates,DCSStates,DCSTransitions, MappedStates, DetectorTransitions, FLESStates, FLESTransitions, QAStates, QATransitions, TFCStates, TFCTransitions,  GlobalSystemTransitions
import configparser
import time
import ECS_tools
import threading
from DataObjects import stateObject
import json
try:
    from django.core.exceptions import ImproperlyConfigured
    #when executed from Django(e.g. in case of Unmmaped Detector Controller)
    from django.conf import settings
    systemPath = settings.PATH_TO_PROJECT+"/"
except ModuleNotFoundError:
    systemPath = ""
except ImproperlyConfigured:
    systemPath = ""


class PartitionComponent:
    def __init__(self,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction):
        configParser = configparser.ConfigParser()
        configParser.read(systemPath+"subsystem.cfg")
        conf = configParser[confSection]
        self.logfunction = logfunction
        self.abort_bool = False
        self.currentStateObject = None
        self.sequenceNumber = 0
        self.config = None
        self.needsReconfiguring = False

        self.pcaReconnectFunction = pcaReconnectFunction
        self.pcaTimeoutFunction = pcaTimeoutFunction

        self.receive_timeout = int(conf["timeout"])
        self.pingInterval = int(conf["pingInterval"])
        self.commandAddress = ("tcp://%s:%s" % (address ,portCommand))

        #zmq Context for Detector
        self.zmqContext = zmq.Context()


        #ping Thread will set Statemachine on connection
        self.connected = None
        self.stateMachine = Statemachine(systemPath+conf["stateFile"],False)

        self.mapper = {}
        with open(systemPath+conf["mapFile"], 'r') as file:
            reader = csv.reader(file, delimiter=',')
            for row in reader:
                if len(row) == 2:
                    self.mapper[row[0]] = row[1]
        start_new_thread(self.ping,())


    def ping(self):
        while True:
            pingSocket = None
            try:
                pingSocket = self.zmqContext.socket(zmq.REQ)
                pingSocket.connect(self.commandAddress)
                pingSocket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
                pingSocket.setsockopt(zmq.LINGER,0)
                pingSocket.send(codes.ping)
                pingSocket.recv()
                if self.connected != True:
                    self.logfunction("%s is connected" % self.name)
                    ret = self.getStateFromSystem()
                    if not ret:
                        #sometimes when PCA and DC start both at once there is a timeout from getting state(maybe the socket isn't ready idk)
                        continue
                    state,configTag = ret
                    self.connected = True
                    self.reconnectFunction(stateObject([self.getMappedStateForState(state),state,configTag,None]))
            except zmq.Again:
                if self.connected == True or self.connected == None:
                    self.connected = False
                    self.logfunction("timeout pinging %s" % self.name, True)
                    self.timeoutFunction()
            except zmq.error.ContextTerminated:
                #termination during sending ping
                break
            except zmq.error.ZMQError:
                break
            finally:
                if pingSocket:
                    pingSocket.close()
            time.sleep(self.pingInterval)

    def checkSequence(self,sequenceNumber):
        """returns True if given number bigger than the current one"""
        #0 is the first client Message therefore reset sequencenumber when 0 is received
        if sequenceNumber > self.sequenceNumber or sequenceNumber == 0:
            self.sequenceNumber = sequenceNumber
            return True
        return False

    def setState(self,stateObj):
        """set the current State"""
        self.stateMachine.currentState = stateObj.unmappedState
        self.currentStateObject = stateObj
        #self.currentStateObject = stateObject([self.getMappedState(),state,configTag,Comment])

    def getStateObject(self):
        """gets current state Object"""
        return self.currentStateObject
        # if not self.connected:
        #     return stateObject(self.getMappedState())
        # else:
        #     return self.currentStateObject

    def getState(self):
        if not self.connected:
            return CommonStates.ConnectionProblem
        return self.stateMachine.currentState

    def getMappedState(self):
        if not self.connected or not self.stateMachine.currentState:
            return CommonStates.ConnectionProblem
        if self.stateMachine.currentState in self.mapper:
            return self.mapper[self.stateMachine.currentState]
        else:
            self.logfunction("Mapped State for %s is not defined" % self.stateMachine.currentState)

    def getMappedStateForState(self,state):
        if state == CommonStates.ConnectionProblem:
            return CommonStates.ConnectionProblem
        if state in self.mapper:
            return self.mapper[state]
        return False

    def getSystemConfig(self):
        return self.config

    def setSystemConfig(self,config):
        self.config = config

class Detector(PartitionComponent):

    def __init__(self,id,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction):
        self.id = id
        self.name = "Detector %s" % id
        super().__init__(address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction)

    def reconnectFunction(self,state):
        self.pcaReconnectFunction(self.id,state)

    def timeoutFunction(self):
        self.pcaTimeoutFunction(self.id)

    def getId(self):
        return self.id

    def createSendSocket(self):
        """init or reset the send Socket"""
        socketSender = self.zmqContext.socket(zmq.REQ)
        socketSender.connect(self.commandAddress)
        socketSender.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
        socketSender.setsockopt(zmq.LINGER,0)
        return socketSender

    def transitionRequest(self,command,sendConfig=False):
        """request a transition from a Detector"""
        self.abort_bool = False
        if not self.connected:
            self.logfunction("Can't transition because Detector %s isn't connected" % self.id)
            return False
        if not self.stateMachine.checkIfPossible(command):
            self.logfunction("Transition %s is not possible for Detector %s in current state" % (command,self.id))
            return False
        try:
            socketSender = self.createSendSocket()
            if sendConfig:
                socketSender.send_multipart([command.encode(),self.config.asJsonString().encode()])
            else:
                socketSender.send_multipart([command.encode()])
            #check if the command has arrived
            #receive status code
            returnMessage = socketSender.recv()
            if returnMessage == codes.busy:
                self.logfunction("Detector %s is busy" % self.id)
                return False
        except zmq.Again:
            self.logfunction("timeout from Detector "+str(self.id)+" for sending "+ command,True)
            return False
        except zmq.error.ContextTerminated:
            self.logfunction("Detector "+str(self.id)+" was terminated during "+ command,True)
            return False
        finally:
            socketSender.close()
        return True

    def getStateFromSystem(self):
        """get's the state from the DetectorController eturns False when a Problem occurs. Use on startup or if there has been a crash or a connection Problem"""
        state = False
        requestSocket = None
        try:
            requestSocket = self.zmqContext.socket(zmq.REQ)
            requestSocket.connect(self.commandAddress)
            requestSocket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
            requestSocket.setsockopt(zmq.LINGER,0)
            requestSocket.send(codes.pcaAsksForDetectorStatus)
            ret = requestSocket.recv_multipart()
            ret = list(map(lambda x:x.decode(),ret))
            configTag = None
            if len(ret) > 1:
                state,configTag = ret
            else:
                state = ret[0]
            return (state,configTag)
        except zmq.Again:
            self.logfunction("timeout getting Detector Status for Detector %s" % (self.id) ,True)
        except Exception as e:
            self.logfunction("error getting Detector Status for Detector %s: %s" % (self.id,str(e)) ,True)
        finally:
            if requestSocket:
                requestSocket.close()

    def terminate(self):
        """ stops the ping thread"""
        self.zmqContext.term()
        self.logfunction("Detector "+str(self.id)+" was terminated",True)

    def error(self):
        if self.getMappedState() in {MappedStates.Error}:
            self.logfunction("nothing to be done for Detector %s" % self.id)
            return True
        return self.transitionRequest(DetectorTransitions.error)

    def reset(self):
        if self.getMappedState() not in {MappedStates.Error}:
            self.logfunction("nothing to be done for Detector %s" % self.id)
            return True
        return self.transitionRequest(DetectorTransitions.reset)

class DetectorA(Detector):

    def getReady(self):
        return self.transitionRequest(DetectorTransitions.configure,sendConfig=True)

    def abort(self):
        if self.getMappedState() == CommonStates.ConnectionProblem or not self.stateMachine.checkIfPossible(DetectorTransitions.abort):
            return False
        return self.transitionRequest(DetectorTransitions.abort)

class DetectorB(Detector):

    def getReady(self):
        if self.getMappedState() not in {MappedStates.Unconfigured, CommonStates.ConnectionProblem,MappedStates.Error}:
            if self.config.configId == self.currentStateObject.configTag:
                self.logfunction("nothing to be done for Detector %s" % self.id)
                return True
        return self.transitionRequest(DetectorTransitions.configure,sendConfig=True)

    def abort(self):
        if self.getMappedState() == CommonStates.ConnectionProblem or not self.stateMachine.checkIfPossible(DetectorTransitions.abort):
            return False
        return self.transitionRequest(DetectorTransitions.abort)

class STS(DetectorA):
    pass

class MVD(DetectorA):
    pass

class TOF(DetectorA):
    pass

class TRD(DetectorA):
    pass

class RICH(DetectorA):
    pass

class GlobalSystemComponent(PartitionComponent):
    def __init__(self,pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction):
        self.pcaId = pcaId
        self.name = "Unset Name"
        super().__init__(address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction)

    def reconnectFunction(self,state):
        self.pcaReconnectFunction(self.name,state)

    def timeoutFunction(self):
        self.pcaTimeoutFunction(self.name)

    def transitionRequest(self,command,sendConfig=False):
        self.abort_bool = False
        if not self.connected:
            self.logfunction("Can't transition because %s isn't connected" % self.name)
            return False
        if not self.stateMachine.currentState:
            return False
        if not self.stateMachine.checkIfPossible(command):
            self.logfunction("Transition %s is not possible for %s in current state" % (command,self.name))
            return False
        socketSender = None
        try:
            socketSender = self.zmqContext.socket(zmq.REQ)
            socketSender.connect(self.commandAddress)
            socketSender.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
            socketSender.setsockopt(zmq.LINGER,0)
            if sendConfig:
                socketSender.send_multipart([command.encode(),self.pcaId.encode(),self.config.asJsonString().encode()])
            else:
                socketSender.send_multipart([command.encode(),self.pcaId.encode()])
            #check if the command has arrived
            #receive status code
            returnMessage = socketSender.recv()
            if returnMessage == codes.busy:
                self.logfunction("%s is busy" % self.name)
                return False
        except zmq.Again:
            self.logfunction("timeout from "+str(self.name)+" for sending "+ command,True)
            return False
        except zmq.error.ContextTerminated:
            self.logfunction(str(self.name)+" was terminated during "+ command,True)
            return False
        finally:
            if socketSender:
                socketSender.close()
        return True

    def getStateFromSystem(self):
        state = False
        requestSocket = None
        try:
            requestSocket = self.zmqContext.socket(zmq.REQ)
            requestSocket.connect(self.commandAddress)
            requestSocket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
            requestSocket.setsockopt(zmq.LINGER,0)
            requestSocket.send_multipart([codes.pcaAsksForDetectorStatus,self.pcaId.encode()])
            ret = requestSocket.recv_multipart()
            ret = list(map(lambda x:x.decode(),ret))
            configTag = None
            if len(ret) > 1:
                state,configTag = ret
            else:
                state = ret[0]
            return (state,configTag)
        except zmq.Again:
            self.logfunction("timeout getting Status for %s" % (self.name) ,True)
        except Exception as e:
            self.logfunction("error getting Status for %s" % (self.name,str(e)) ,True)
            raise e
        finally:
            if requestSocket:
                requestSocket.close()

    def reset(self):
        if self.getMappedState() not in {MappedStates.Error}:
            self.logfunction("nothing to be done for %s" % self.id)
            return True
        return self.transitionRequest(GlobalSystemTransitions.reset)

class DCS(GlobalSystemComponent):
    def __init__(self,pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction):
        super().__init__(pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction)
        self.name = "DCS"

    def getReady(self):
        return self.transitionRequest(DCSTransitions.configure,sendConfig=True)

    def abort(self):
        if not self.stateMachine.checkIfPossible(DCSTransitions.abort):
            return False
        return self.transitionRequest(DCSTransitions.abort)

class TFC(GlobalSystemComponent):
    def __init__(self,pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction):
        super().__init__(pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction)
        self.name = "TFC"

    def getReady(self):
        return self.transitionRequest(TFCTransitions.configure,sendConfig=True)

    def abort(self):
        if not self.stateMachine.checkIfPossible(TFCTransitions.abort):
            return False
        return self.transitionRequest(TFCTransitions.abort)

class QA(GlobalSystemComponent):
    def __init__(self,pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction):
        super().__init__(pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction)
        self.name = "QA"

    def startRecording(self):
        if self.stateMachine.currentState == QAStates.Active:
            return self.transitionRequest(QATransitions.start)
        return False

    def stopRecording(self):
        if self.stateMachine.currentState == QAStates.Recording:
            return self.transitionRequest(QATransitions.stop)
        return False

    def getReady(self):
        return self.transitionRequest(QATransitions.configure,sendConfig=True)

    def abort(self):
        if not self.stateMachine.checkIfPossible(QATransitions.abort):
            return False
        return self.transitionRequest(QATransitions.abort)

class FLES(GlobalSystemComponent):
    def __init__(self,pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction):
        super().__init__(pcaId,address,portCommand,confSection,logfunction,pcaTimeoutFunction,pcaReconnectFunction)
        self.name = "FLES"

    def startRecording(self):
        if self.stateMachine.currentState == FLESStates.Active:
            return self.transitionRequest(FLESTransitions.start)
        return False

    def stopRecording(self):
        if self.stateMachine.currentState == FLESStates.Recording:
            return self.transitionRequest(FLESTransitions.stop)
        return False

    def getReady(self):
        return self.transitionRequest(FLESTransitions.configure,sendConfig=True)

    def abort(self):
        if not self.stateMachine.checkIfPossible(FLESTransitions.abort):
            return False
        return self.transitionRequest(FLESTransitions.abort)

class DetectorTypes:
    classList = {
        "DetectorA" : DetectorA,
        "DetectorB" : DetectorB,
        "TRD" : TRD,
        "STS" : STS,
        "MVD" : MVD,
        "TOF" : TOF,
        "RICH" : RICH,
    }

    confSection = {
        "DetectorA" : "DETECTOR_A",
        "DetectorB" : "DETECTOR_B",
        "TRD" : "TRD",
        "STS" : "STS",
        "MVD" : "MVD",
        "TOF" : "TOF",
        "RICH" : "RICH",
    }

    def getClassForType(self,type):
        if type in self.classList:
            return self.classList[type]
        else:
            return None

    def getConfsectionForType(self,type):
        if type in self.confSection:
            return self.confSection[type]
        else:
            return None
