#!/usr/bin/python3
import zmq
import logging
import threading
from ECSCodes import ECSCodes
codes = ECSCodes()
import struct
import json
from multiprocessing import Queue
import time
import ECS_tools
from datetime import datetime
import paramiko
from  UnmappedDetectorController import UnmappedDetectorController
from GUI.models import pcaModel, ecsModel
from django.conf import settings
from collections import deque
from django.utils import timezone
from DataObjects import DataObjectCollection, detectorDataObject, partitionDataObject, stateObject, globalSystemDataObject, DataObject, configObject
import asyncio
import signal
from states import PCAStates
PCAStates = PCAStates()
from DataBaseWrapper import DataBaseWrapper
from WebSocket import WebSocket

class ECA:
    """The Experiment Control Agent"""
    def __init__(self):
        #data stuff
        self.database = DataBaseWrapper(self.log)
        self.partitions = ECS_tools.MapWrapper()
        self.disconnectedDetectors = ECS_tools.MapWrapper()
        self.stateMap = ECS_tools.MapWrapper()
        self.logQueue = deque(maxlen=settings.BUFFERED_LOG_ENTRIES)

        #set settings
        self.receive_timeout = settings.TIMEOUT
        self.pingInterval = settings.PINGINTERVAL
        self.pingTimeout = settings.PINGTIMEOUT
        self.pathToPCACodeFile = settings.PATH_TO_PROJECT
        self.pathToDetectorCodeFile = settings.PATH_TO_PROJECT
        self.pathToGlobalSystemFile = settings.PATH_TO_PROJECT
        self.DetectorCodeFileName = settings.DETECTOR_CODEFILE_NAME
        self.PCACodeFileName = settings.PCA_CODEFILE_NAME
        self.globalSystemFileName = settings.GLOBALSYSTEM_CODE_FILE
        self.checkIfRunningScript = settings.CHECK_IF_RUNNING_SCRIPT
        self.virtenvFile =  settings.PYTHON_VIRTENV_ACTIVATE_FILE

        #zmq context with timeouts
        self.zmqContext = zmq.Context()
        self.zmqContext.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
        self.zmqContext.setsockopt(zmq.LINGER,0)

        #zmq context without timeouts
        self.zmqContextNoTimeout = zmq.Context()
        self.zmqContextNoTimeout.setsockopt(zmq.LINGER,0)

        #socket for receiving requests
        self.replySocket = self.zmqContextNoTimeout.socket(zmq.REP)
        self.replySocket.bind("tcp://*:%s" % settings.ECA_REQUEST_PORT)

        #log publish socket
        self.socketLogPublish = self.zmqContext.socket(zmq.PUB)
        self.socketLogPublish.bind("tcp://*:%s" % settings.ECA_LOG_PORT)

        #init logger
        self.logfile = settings.LOG_PATH_ECS
        debugMode = settings.DEBUG
        logging.basicConfig(
            format='%(asctime)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p',
            handlers=[
            #logging to file
            logging.FileHandler(self.logfile),
            #logging on console and WebUI
            logging.StreamHandler()
        ])
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger().handlers[0].setLevel(logging.INFO)
        #disable info logging for paramiko
        logging.getLogger("paramiko").setLevel(logging.WARNING)

        #set console log to info level if in debug mode
        if debugMode:
            logging.getLogger().handlers[1].setLevel(logging.INFO)
        else:
            logging.getLogger().handlers[1].setLevel(logging.CRITICAL)

        self.webSocket = WebSocket(settings.ECS_ADDRESS,settings.WEB_SOCKET_PORT)

        #Information for UnmappedDetectorController
        data = {
            "id" : "unmapped",
            "address" : settings.ECS_ADDRESS,
            "portPublish" : settings.UNUSED_DETECTORS_PUBLISH_PORT,
            "portLog" : "-1",
            "portUpdates" : settings.UNUSED_DETECTORS_UPDATES_PORT,
            "portCurrentState" : settings.UNUSED_DETECTORS_CURRENT_STATE_PORT,
            "portCommand" : "-1",

        }
        self.unmappedDetectorControllerData = partitionDataObject(data)

        systemList = ["TFC","DCS","QA","FLES"]
        res = []
        #get info from database
        for s in systemList:
            res.append(self.database.getGlobalSystem(s))

        #Store GlobalSystem Information
        tfcData, dcsData, qaData, flesData = res
        self.globalSystems = {}
        self.globalSystems["TFC"] = tfcData
        self.globalSystems["DCS"] = dcsData
        self.globalSystems["QA"] = qaData
        self.globalSystems["FLES"] = flesData

        t = threading.Thread(name="requestHandler", target=self.waitForRequests)
        t.start()

        partitions = self.database.getAllPartitions()
        if isinstance(partitions,Exception):
            raise partitions
        for p in partitions:
            self.partitions[p.id] = p
        #start PCA clients via ssh
        if settings.START_CLIENTS:
            for p in self.partitions:
                ret = self.startClient(p)

        #clear permissions in database from previous runs
        pcaModel.objects.all().delete()
        ecsModel.objects.all().delete()

        self.pcaHandlers = {}
        #create Handlers for PCAs
        for p in self.partitions:
            self.pcaHandlers[p.id] = PCAHandler(p,self.log,self.globalSystems,self.webSocket)
            #add database object for storing user permissions
            pcaModel.objects.create(id=p.id,permissionTimestamp=timezone.now())
        #user Permissions ecs
        ecsModel.objects.create(id="ecs",permissionTimestamp=timezone.now())

        self.terminate = False

        #create Controller for Unmapped Detectors
        self.unmappedStateTable = ECS_tools.MapWrapper()
        unmappedDetectors = self.database.getAllUnmappedDetectors()
        self.unmappedDetectorController = UnmappedDetectorController(unmappedDetectors,self.unmappedDetectorControllerData.portPublish,self.unmappedDetectorControllerData.portUpdates,self.unmappedDetectorControllerData.portCurrentState,self.log,self.webSocket)

        #currently unused
        t = threading.Thread(name="consistencyCheckThread", target=self.consistencyCheckThread)
        #t.start()

    def getPCAHandler(self,id):
        """get pca handler for given id"""
        if id in self.pcaHandlers:
            return self.pcaHandlers[id]
        else:
            return None

    def consistencyCheckThread(self):
        """checks wether states and Detector Assignment is Consistend between all Clients"""
        while True:
            time.sleep(5)
            if self.terminate:
                break
            self.checkSystemConsistency()

    def checkIfRunning(self,clientObject):
        """check if a client is Running"""
        if isinstance(clientObject,detectorDataObject):
            path = self.pathToDetectorCodeFile
        elif isinstance(clientObject,globalSystemDataObject):
            path = self.pathToGlobalSystemFile
        elif isinstance(clientObject,partitionDataObject):
            path = self.pathToPCACodeFile
        else:
            raise Exception("Expected detector or partition Object but got %s" % type(clientObject))
        id = clientObject.id
        address = clientObject.address
        fileName = self.checkIfRunningScript
        ssh = None
        try:
            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()
            #login on pca computer needs a rsa key
            ssh.connect(address)
            #python script returns -1 if not running or the pid otherwise
            stdin, stdout, stderr = ssh.exec_command("pid=$(cd %s;source %s; python %s %s); echo $pid" % (path,self.virtenvFile,fileName,id))
            pid = stdout.readline()
            pid = pid.strip()
            ssh.close()
            if pid == "-1":
                return False
            return pid
        except Exception as e:
            if ssh:
                ssh.close()
            self.log("Exception executing ssh command: %s" % str(e))
            return False

    def startClient(self,clientObject):
        """starts a PCA or controller agent client via SSH and returns the process Id on success"""
        if isinstance(clientObject,detectorDataObject):
            path = self.pathToDetectorCodeFile
            fileName = self.DetectorCodeFileName
        elif isinstance(clientObject,globalSystemDataObject):
            path = self.pathToGlobalSystemFile
            fileName = self.globalSystemFileName
        elif isinstance(clientObject,partitionDataObject):
            path = self.pathToPCACodeFile
            fileName = self.PCACodeFileName
        else:
            raise Exception("Expected detector, partition or globalSystem Object but got %s" % type(clientObject))
        id = clientObject.id
        address = clientObject.address
        ssh = None
        try:
            pid = self.checkIfRunning(clientObject)
            if not pid:
                ssh = paramiko.SSHClient()
                ssh.load_system_host_keys()
                #login on pca computer needs a rsa key
                ssh.connect(address)
                #start Client and get its pid
                stdin, stdout, stderr = ssh.exec_command("cd %s;source %s;python %s %s > /dev/null & echo $!" % (path,self.virtenvFile,fileName,id))
                pid = stdout.readline()
                pid = pid.strip()
                ssh.close()
            else:
                self.log("Client %s is already Running with PID %s" % (id,pid))
            return pid
        except Exception as e:
            if ssh:
                ssh.close()
            self.log("Exception executing ssh command: %s" % str(e))
            return False

    def stopClient(self,clientObject):
        """kills a PCA or controller agent client via SSH"""
        if not (isinstance(clientObject,detectorDataObject) or isinstance(clientObject,partitionDataObject) or isinstance(clientObject,globalSystemDataObject)):
            raise Exception("Expected detector, partition or globalSystem Object but got %s" % type(clientObject))
        id = clientObject.id
        address = clientObject.address
        pid = self.checkIfRunning(clientObject)
        if not pid:
            self.log("tried to stop %s Client but it wasn't running" % id)
            return True
        ssh = None
        try:
            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()
            ssh.connect(address)
            stdin, stdout, stderr = ssh.exec_command("kill %s" % (pid,))
            returnValue = stdout.channel.recv_exit_status()
            ssh.close()
            if returnValue == 0:
                return True
            else:
                self.log("error stopping Client for %s" % id)
                return False
        except Exception as e:
            if ssh:
                ssh.close()
            self.log("Exception executing ssh command: %s" % str(e))
            return False

    def createPartition(self,partition):
        """ Create a new Partition and start it's PCA Client"""
        ret = self.database.addPartition(partition)
        if ret == codes.ok:
            #inform GlobalSystems
            informedSystems = []
            for gsID in self.globalSystems:
                try:
                    gs = self.globalSystems[gsID]
                    requestSocket = self.zmqContext.socket(zmq.REQ)
                    requestSocket.connect("tcp://%s:%s"  % (gs.address,gs.portCommand))
                    requestSocket.send_multipart([codes.addPartition,partition.asJsonString().encode()])
                    ret = requestSocket.recv()
                    if ret != codes.ok:
                        raise Exception("Global System returned ErrorCode")
                    informedSystems.append(gsID)
                except Exception as e:
                    #start rollback(tell all informed Global Systems to remove the new partition)
                    ret = self.database.removePartition(partition.id)
                    requestSocket.close()
                    for gsIDRolback in informedSystems:
                        gs = self.globalSystems[gsIDRolback]
                        requestSocket = self.zmqContext.socket(zmq.REQ)
                        requestSocket.connect("tcp://%s:%s"  % (gs.address,gs.portCommand))
                        requestSocket.send_multipart([codes.deletePartition,partition.id.encode()])
                        ret = requestSocket.recv()
                        if ret != codes.ok:
                            raise Exception("Global System returned ErrorCode during rollback")
                    if isinstance(e,zmq.Again):
                        self.log("timeout informing Global System %s" % (gsID),True)
                        return "timeout informing Global System %s" % (gsID)
                    else:
                        self.log("error informing Global System %s: %s" % (gsID),str(e),True)
                        return "error informing Global System %s: %s " % (gsID,str(e))
                finally:
                    requestSocket.close()


            #start PCA if Not Running
            if settings.START_CLIENTS:
                pid = self.checkIfRunning(partition)
                if not pid:
                    pid = self.startClient(partition)
                if not pid:
                    self.log("PCA Client for %s could not be startet" % partition.id,True)


            #connect to pca
            self.partitions[partition.id] = partition
            self.pcaHandlers[partition.id] = PCAHandler(partition,self.log,self.globalSystems,self.webSocket)
            #add database object for storing user permissions
            pcaModel.objects.create(id=partition.id,permissionTimestamp=timezone.now())
            return True
        else:
            return str(ret)

    def deletePartition(self,pcaId,forceDelete=False):
        """delete a Partition on stop it's client"""
        try:
            partition = self.database.getPartition(pcaId)
            if isinstance(partition,Exception):
                return str(partition)
            elif partition == codes.idUnknown:
                return "Partition with id %s not found" % pcaId
            detectors = self.database.getDetectorsForPartition(pcaId)
            if isinstance(detectors,Exception):
                return str(detectors)

            #check if there are Detectors still assigned to the Partition
            if len(detectors.asDictionary()) > 0:
                raise Exception("Can not delete because there are still Detectors assigned to Partition")
            ret = self.database.removePartition(pcaId)
            if isinstance(ret,Exception):
                return str(ret)

            #inform Global Systems
            informedSystems=[]
            for gsID in self.globalSystems:
                try:
                    gs = self.globalSystems[gsID]
                    requestSocket = self.zmqContext.socket(zmq.REQ)
                    requestSocket.connect("tcp://%s:%s"  % (gs.address,gs.portCommand))
                    requestSocket.send_multipart([codes.deletePartition,partition.id.encode()])
                    ret = requestSocket.recv()
                    if ret != codes.ok:
                        raise Exception("Global System returned ErrorCode")
                    informedSystems.append(gsID)
                except Exception as e:
                    #start rollback
                    self.database.addPartition(partition)
                    requestSocket.close()
                    for gsIDRolback in informedSystems:
                        gs = self.globalSystems[gsIDRolback]
                        requestSocket = self.zmqContext.socket(zmq.REQ)
                        requestSocket.connect("tcp://%s:%s"  % (gs.address,gs.portCommand))
                        requestSocket.send_multipart([codes.addPartition,partition.asJsonString().encode()])
                        ret = requestSocket.recv()
                        if ret != codes.ok:
                            raise Exception("Global System returned ErrorCode during rollback")
                    if isinstance(e,zmq.Again):
                        self.log("timeout informing Global System %s" % (gsID),True)
                        if not forceDelete:
                            return "timeout informing Global System %s" % (gsID)
                    else:
                        self.log("error informing Global System %s: %s" % (gsID),str(e),True)
                        if not forceDelete:
                            return "error informing Global System %s: %s " % (gsID,str(e))
                finally:
                    requestSocket.close()
            #try to stop pca Client
            self.stopClient(partition)
            #remove from Maps
            del self.partitions[partition.id]
            #terminate pcaHandler
            self.pcaHandlers[pcaId].terminatePCAHandler()
            del self.pcaHandlers[pcaId]
            #delete PCA Permissions
            pcaModel.objects.get(id=pcaId).delete()

            return True
        except Exception as e:
            self.log("Error Deleting Partition: %s" % str(e))
            return str(e)

    def createDetector(self,dataObject):
        """create a new Detector"""
        dbChanged = False
        try:
            ret = self.database.addDetector(dataObject)
            if ret == codes.ok:
                dbChanged = True
                if self.unmappedDetectorController.checkIfTypeIsKnown(dataObject):
                    ret = self.unmappedDetectorController.addDetector(dataObject)
                    return True
                else:
                    raise Exception("Detector Type is unknown")
            else:
                if isinstance(ret,Exception):
                    return str(ret)
                else:
                    return "Database error"
        except Exception as e:
            if dbChanged:
                self.database.removeDetector(dataObject.id)
            raise e
            return str(e)

    def deleteDetector(self,detId,forceDelete=False):
        """deletes Detector enirely from System trys to shutdown the Detector;
         with forceDelete User has the possibillity to delete from database without shutting down the detector;
         only unmapped Detectors can be deleted"""
        detector = self.database.getDetector(detId)
        if isinstance(detector,Exception):
            return str(detector)
        if detector == codes.idUnknown:
            return "Detector Id is unknown"
        if not forceDelete:
            #add to UnmappedDetectorController
            if not self.unmappedDetectorController.isDetectorConnected(detector.id):
                return "Detector %s is not connected" % detector.id
            if not self.unmappedDetectorController.abortDetector(detector.id):
                self.log("Detector %s could not be aborted" % (detector.id))
                return Exception("Detector %s could not be aborted" % (detector.id))

        self.unmappedDetectorController.removeDetector(detector.id)
        #delete from Database
        ret = self.database.removeDetector(detector.id)
        if ret != codes.ok:
            return "Error removing Detector %s from database" % detector.id
        return True

    def getAllSystems(self):
        """get all detectors and global systems; returns an dictionary with keys 'detectors' and 'globalSystems' """
        detectors = self.database.getAllDetectors()
        if isinstance(detectors,Exception):
            return detectors
        globalSystems = self.database.getAllGlobalSystems()
        if isinstance(globalSystems,Exception):
            return globalSystems
        ret = {
            "detectors": detectors,
            "globalSystems": globalSystems,
        }
        return ret

    def moveDetector(self,detectorId,partitionId,forceMove=False):
        """moves a Detector between Partitions"""
        removed = False
        added = False
        dbChanged = False
        #is not assigned to any partition(unmapped)
        unused = False
        informedSystems=[]

        skipUnmap = False
        skipAdd = False

        oldPartition = self.database.getPartitionForDetector(detectorId)
        if oldPartition == codes.idUnknown:
            unused = True
        else:
            if not self.pcaHandlers[oldPartition.id].PCAConnection:
                #skip informing PCA if its not connected and forceMove is True
                if forceMove:
                    skipUnmap = True
                else:
                    return "Partition %s is not connected" % oldPartition.id
        if partitionId == "unmapped":
            #detector will be unmapped
            newPartition = False
        else:
            if not self.pcaHandlers[partitionId].PCAConnection:
                if forceMove:
                    skipAdd = True
                else:
                    return "Partition %s is not connected" % partitionId
            newPartition = self.partitions[partitionId]

        try:
            lockedPartitions = []
            detector = self.database.getDetector(detectorId)
            #change Database
            if unused:
                if self.database.mapDetectorToPCA(detectorId,newPartition.id) != codes.ok:
                    raise Exception("Error during changing Database")
            elif not newPartition:
                if self.database.unmapDetectorFromPCA(detectorId) != codes.ok:
                    raise Exception("Error during changing Database")
            else:
                if self.database.remapDetector(detectorId,newPartition.id,oldPartition.id) != codes.ok:
                    raise Exception("Error during changing Database")
            dbChanged = True

            #lock partitions(PCAs don't accept commands while locked)
            partitionsToLock = []
            if not unused:
                partitionsToLock.append(oldPartition)
            if newPartition:
                partitionsToLock.append(newPartition)
            for p in partitionsToLock:
                try:
                    requestSocket = self.zmqContext.socket(zmq.REQ)
                    requestSocket.connect("tcp://%s:%s"  % (p.address,p.portCommand))
                    requestSocket.send_multipart([codes.lock])
                    ret = requestSocket.recv()
                    if ret != codes.ok:
                        self.log("%s returned error for locking Partition" % (p.id),True)
                        raise Exception("%s returned error for locking Partition" % (p.id))
                    lockedPartitions.append(p)
                except zmq.Again:
                    self.log("timeout locking Partition %s" % (p.id),True)
                    raise Exception("timeout locking Partition %s" % (p.id))
                except Exception as e:
                    self.log("error locking Partition %s: %s " % (p.id,str(e)),True)
                    raise Exception("error locking Partition %s: %s " % (p.id,str(e)))
                finally:
                    requestSocket.close()

            if not unused:
                if not skipUnmap:
                    #remove from Old Partition
                    try:
                        requestSocket = self.zmqContext.socket(zmq.REQ)
                        requestSocket.connect("tcp://%s:%s"  % (oldPartition.address,oldPartition.portCommand))
                        requestSocket.send_multipart([codes.removeDetector,detectorId.encode()])
                        ret = requestSocket.recv()
                        if ret == codes.busy:
                            self.log("%s is not in Idle State" % (oldPartition.id),True)
                            raise Exception("%s is not in Idle State" % (oldPartition.id))
                        elif ret != codes.ok:
                            self.log("%s returned error for removing Detector" % (oldPartition.id),True)
                            raise Exception("%s returned error for removing Detector" % (oldPartition.id))
                    except zmq.Again:
                        self.log("timeout removing Detector from %s" % (oldPartition.id),True)
                        raise Exception("timeout removing Detector from %s" % (oldPartition.id))
                    except Exception as e:
                        self.log("error removing Detector from %s: %s " % (oldPartition.id,str(e)),True)
                        raise Exception("error removing Detector from %s: %s " % (oldPartition.id,str(e)))
                    finally:
                        requestSocket.close()
            else:
                #remove from Unused Detectors
                self.unmappedDetectorController.removeDetector(detectorId)
            removed = True

            if newPartition:
                if not skipAdd:
                    try:
                        #add to new Partition
                        requestSocket = self.zmqContext.socket(zmq.REQ)
                        requestSocket.connect("tcp://%s:%s"  % (newPartition.address,newPartition.portCommand))
                        requestSocket.send_multipart([codes.addDetector,detector.asJsonString().encode()])
                        ret = requestSocket.recv()
                        if ret == codes.busy:
                            self.log("%s is not in Idle State" % (newPartition.id),True)
                            raise Exception("%s is not in Idle State" % (newPartition.id))
                        elif ret != codes.ok:
                            self.log("%s returned error for adding Detector" % (newPartition.id),True)
                            raise Exception("%s returned error for adding Detector" % (newPartition.id))
                    except zmq.Again:
                        self.log("timeout adding Detector to %s" % (newPartition.id),True)
                        raise Exception("timeout adding Detector to %s" % (newPartition.id))
                    except Exception as e:
                        self.log("error adding Detector to %s: %s " % (newPartition.id,str(e)),True)
                        raise Exception("error adding Detector to %s: %s " % (newPartition.id,str(e)))
                    finally:
                        requestSocket.close()
            else:
                #add to unused detectors
                self.unmappedDetectorController.addDetector(detector)
            added = True

            #inform GlobalSystems
            for gsID in self.globalSystems:
                try:
                    gs = self.globalSystems[gsID]
                    requestSocket = self.zmqContext.socket(zmq.REQ)
                    requestSocket.connect("tcp://%s:%s"  % (gs.address,gs.portCommand))
                    if newPartition:
                        requestSocket.send_multipart([codes.remapDetector,newPartition.id.encode(),detector.id.encode()])
                    else:
                        requestSocket.send_multipart([codes.remapDetector,codes.removed,detector.id.encode()])
                    ret = requestSocket.recv()
                    if ret != codes.ok:
                        raise Exception("Global System returned ErrorCode")
                    informedSystems.append(gsID)
                except zmq.Again:
                    self.log("timeout informing Global System %s" % (gsID),True)
                    raise Exception("timeout informing Global System %s" % (gsID))
                except Exception as e:
                    self.log("error informing Global System %s: %s " % (gsID,str(e)),True)
                    raise Exception("error informing Global System %s: %s " % (gsID,str(e)))
                finally:
                    requestSocket.close()

            #inform DetectorController
            requestSocket = self.zmqContext.socket(zmq.REQ)
            requestSocket.connect("tcp://%s:%s"  % (detector.address,detector.portCommand))
            if newPartition:
                requestSocket.send_multipart([codes.detectorChangePartition,newPartition.asJsonString().encode()])
            else:
                requestSocket.send_multipart([codes.detectorChangePartition,self.unmappedDetectorControllerData.asJsonString().encode()])
            try:
                ret = requestSocket.recv()
                if ret != codes.ok:
                    self.log("%s returned error for changing PCA" % (detector.id),True)
                    raise Exception("%s returned error for changing PCA" % (detector.id,))
            except zmq.Again:
                self.log("timeout informing Detector %s" % (detector.id),True)
                if not forceMove:
                    raise Exception("timeout informing Detector %s" % (detector.id))
            except Exception as e:
                self.log("error changing Detector %s PCA: %s " % (detector.id),str(e),True)
                raise Exception("error changing Detector %s PCA: %s " % (detector.id),str(e))
            finally:
                requestSocket.close()
            #unlock partitions
            for p in lockedPartitions:
                try:
                    requestSocket = self.zmqContext.socket(zmq.REQ)
                    requestSocket.connect("tcp://%s:%s"  % (p.address,p.portCommand))
                    requestSocket.send_multipart([codes.unlock])
                    ret = requestSocket.recv()
                    if ret != codes.ok:
                        self.log("%s returned error for unlocking Partition" % (p.id),True)
                        raise Exception("%s returned unlocking Partition" % (p.id))
                except zmq.Again:
                    self.log("timeout unlocking Partition %s" % (p.id),True)
                    raise Exception("timeout unlocking Partition %s" % (p.id))
                except Exception as e:
                    self.log("error unlocking Partition %s: %s " % (p.id,str(e)),True)
                    raise Exception("error unlocking Partition %s: %s " % (p.id,str(e)))
                finally:
                    requestSocket.close()

            return True
        except Exception as e:
            self.log("error during remapping:%s ;starting rollback for remapping Detector" % str(e),True)
            #rollback
            try:
                if dbChanged:
                    if unused:
                        if self.database.unmapDetectorFromPCA(detectorId) != codes.ok:
                            raise Exception("Error during changing Database")
                    elif not newPartition:
                        if self.database.mapDetectorToPCA(detectorId,oldPartition.id) != codes.ok:
                            raise Exception("Error during changing Database")
                    else:
                        if self.database.remapDetector(detectorId,oldPartition.id,newPartition.id) != codes.ok:
                            raise Exception("Error during changing Database")

                #inform GlobalSystems
                for gsID in informedSystems:
                    try:
                        gs = self.globalSystems[gsID]
                        requestSocket = self.zmqContext.socket(zmq.REQ)
                        requestSocket.connect("tcp://%s:%s"  % (gs.address,gs.portCommand))
                        if not unused:
                            requestSocket.send_multipart([codes.remapDetector,oldPartition.id.encode(),detector.id.encode()])
                        else:
                            requestSocket.send_multipart([codes.remapDetector,codes.removed,detector.id.encode()])
                        ret = requestSocket.recv()
                        if ret != codes.ok:
                            raise Exception("Global System returned ErrorCode")
                    except zmq.Again:
                        self.log("timeout informing Global System %s" % (gsID),True)
                        raise Exception("timeout informing Global System %s" % (gsID))
                    except Exception as e:
                        self.log("error informing Global System %s: %s " % (gsID,str(e)),True)
                        raise Exception("error informing Global System %s: %s " % (gsID,str(e)))
                    finally:
                        requestSocket.close()

                if removed:
                    if not unused:
                        requestSocket = self.zmqContext.socket(zmq.REQ)
                        requestSocket.connect("tcp://%s:%s"  % (oldPartition.address,oldPartition.portCommand))
                        requestSocket.send_multipart([codes.addDetector,detector.asJsonString().encode()])
                        try:
                            ret = requestSocket.recv()
                            requestSocket.close()
                        except zmq.Again as e:
                            self.log("timeout adding Detector to %s" % (oldPartition.id),True)
                            requestSocket.close()
                            raise Exception("timeout during adding for pca:%s" % oldPartition.id )
                        except Exception as e:
                            self.log("error adding Detector to %s: %s " % (oldPartition.id,str(e)),True)
                            requestSocket.close()
                            raise Exception("Error during adding :%s" % str(e) )
                        if ret == codes.busy:
                            self.log("%s is not in Idle State" % (oldPartition.id),True)
                            raise Exception("%s is not in Idle State" % (oldPartition.id))
                        elif ret != codes.ok:
                            self.log("%s returned error for adding Detector" % (oldPartition.id),True)
                            requestSocket.close()
                            raise Exception("pca returned error code during adding Detector")
                        requestSocket.close()
                    else:
                        #add to unused detectors
                        self.unmappedDetectorController.addDetector(detector)
                if added:
                    if newPartition:
                        try:
                            requestSocket = self.zmqContext.socket(zmq.REQ)
                            requestSocket.connect("tcp://%s:%s"  % (newPartition.address,newPartition.portCommand))
                            requestSocket.send_multipart([codes.removeDetector,detectorId.encode()])
                            ret = requestSocket.recv()
                        except zmq.Again:
                            self.log("timeout removing Detector from %s" % (newPartition.id),True)
                            requestSocket.close()
                            raise Exception("timeout removing Detector from %s" % (newPartition.id))
                        except Exception:
                            self.log("error removing Detector from %s: %s " % (newPartition.id,str(e)),True)
                            requestSocket.close()
                            raise Exception("error removing Detector from %s: %s " % (newPartition.id,str(e)))
                        finally:
                            requestSocket.close()
                        if ret == codes.busy:
                            self.log("%s is not in Idle State" % (newPartition.id),True)
                            raise Exception("%s is not in Idle State" % (newPartition.id))
                        elif ret != codes.ok:
                                self.log("%s returned error for removing Detector" % (newPartition.id),True)
                                raise Exception("%s returned error for removing Detector" % (oldPartition.id))
                    else:
                        #remove from Unused Detectors
                        self.unmappedDetectorController.removeDetector(detectorId)

                #unlock partitions
                for p in lockedPartitions:
                    try:
                        requestSocket = self.zmqContext.socket(zmq.REQ)
                        requestSocket.connect("tcp://%s:%s"  % (p.address,p.portCommand))
                        requestSocket.send_multipart([codes.unlock])
                        ret = requestSocket.recv()
                        if ret != codes.ok:
                            self.log("%s returned error for unlocking Partition" % (p.id),True)
                            raise Exception("%s returned unlocking Partition" % (p.id))
                    except zmq.Again:
                        self.log("timeout unlocking Partition %s" % (p.id),True)
                        raise Exception("timeout unlocking Partition %s" % (p.id))
                    except Exception as e:
                        self.log("error unlocking Partition %s: %s " % (p.id,str(e)),True)
                        raise Exception("error unlocking Partition %s: %s " % (p.id,str(e)))
                    finally:
                        requestSocket.close()
                    return str(e)
            except Exception as e:
                self.log("Exception during roll back %s" %str(e),True)
                return str(e)

    def partitionForDetector(self,detId):
        """returns partition data for detector or data of UnmappedDetectorController if it's unassigned"""
        ret = self.database.getPartitionForDetector(detId)
        if ret != codes.idUnknown:
            return ret
        else:
            return self.unmappedDetectorControllerData

    def waitForRequests(self):
        """waits for client Requests"""
        while True:
            try:
                m = self.replySocket.recv_multipart()
            except zmq.error.ContextTerminated:
                self.replySocket.close()
                break
            arg = None
            if len(m) == 2:
                code, arg = m
                arg = arg.decode()
            elif len(m) == 1:
                code = m[0]
            else:
                self.log("received malformed request message: %s", str(m),True)
                continue

            def switcher(code,arg=None):
                #functions for codes
                dbFunctionDictionary = {
                    codes.pcaAsksForConfig: self.database.getPartition,
                    codes.detectorAsksForPCA: self.partitionForDetector,
                    codes.getDetectorForId: self.database.getDetector,
                    codes.pcaAsksForDetectorList: self.database.getDetectorsForPartition,
                    codes.getPartitionForId: self.database.getPartition,
                    codes.getAllPCAs: self.database.getAllPartitions,
                    codes.getUnmappedDetectors: self.database.getAllUnmappedDetectors,
                    codes.GlobalSystemAsksForInfo: self.database.getGlobalSystem,
                    codes.getDetectorMapping: self.database.getDetectorMapping,
                }
                #returns function for Code or None if the received code is unknown
                f = dbFunctionDictionary.get(code,None)
                try:
                    if not f:
                        self.log("received unknown command",True)
                        self.replySocket.send(codes.unknownCommand)
                        return
                    if arg:
                        ret = f(arg)
                    else:
                        ret = f()
                    #is result a Dataobject?
                    if isinstance(ret,DataObject) or isinstance(ret,DataObjectCollection):
                        #encode Dataobject
                        ret = ret.asJsonString().encode()
                        self.replySocket.send(ret)
                    elif isinstance(ret,Exception):
                        #it's an error message
                        self.replySocket.send(codes.error)
                    else:
                        #it's just a returncode
                        self.replySocket.send(ret)
                except zmq.error.ContextTerminated:
                    self.replySocket.close()
                    return
            if arg:
                switcher(code,arg)
            else:
                switcher(code)

    def checkPartition(self,partition):
        """checks is Partition has the corrent DetectorList"""
        detectors = self.database.getDetectorsForPartition(partition.id)
        if isinstance(detectors,Exception):
            raise detectors
        if detectors == codes.idUnknown:
            Raise(Exception("Partition %s is not in database") % partition.id)
        try:
            #for whatever reason this raises a different Exception for ContextTerminated than send or recv
            requestSocket = self.zmqContext.socket(zmq.REQ)
        except zmq.error.ZMQError:
            return
        try:
            requestSocket.connect("tcp://%s:%s"  % (partition.address,partition.portCommand))
            requestSocket.send_multipart([codes.check,detectors.asJsonString().encode()])
            ret = requestSocket.recv()
        except zmq.Again:
            handler = self.getPCAHandler(partition.id)
            handler.handleDisconnection()
        except zmq.error.ContextTerminated:
            pass
        except Exception as e:
            self.log("error checking PCA %s: %s " % (partition.id,str(e)),True)
        finally:
            requestSocket.close()

    def checkDetector(self,detector):
        """check if Detector has the correct Partition"""
        partition = self.database.getPartitionForDetector(detector.id)
        if isinstance(partition,Exception):
            raise detectors
        if partition == codes.idUnknown:
            partition = self.unmappedDetectorControllerData
        try:
            #for whatever reason this raises a different Exception for ContextTerminated than send or recv
            requestSocket = self.zmqContext.socket(zmq.REQ)
        except zmq.error.ZMQError:
            return
        try:
            requestSocket = self.zmqContext.socket(zmq.REQ)
            requestSocket.connect("tcp://%s:%s"  % (detector.address,detector.portCommand ))
            requestSocket.send_multipart([codes.check,partition.asJsonString().encode()])
            ret = requestSocket.recv()
            if detector.id in self.disconnectedDetectors:
                del self.disconnectedDetectors[detector.id]
        except zmq.Again:
            if detector.id not in self.disconnectedDetectors:
                self.log("timeout checking Detector %s" % (detector.id),True)
                self.disconnectedDetectors[detector.id] = detector.id
        except zmq.error.ContextTerminated:
            pass
        except Exception as e:
            self.log("error checking Detector %s: %s " % (detector.id,str(e)),True)
        finally:
            requestSocket.close()

    def checkSystemConsistency(self):
        """check if Detector assignmet is correct"""
        unmappedDetectors = self.database.getAllUnmappedDetectors()
        for d in unmappedDetectors:
            if d.id not in self.unmappedDetectorController.detectors:
                self.log("System check: Detector %s should have been in unmapped Detectors" % d.id,True)
                self.unmappedDetectorController.addDetector(d)
        for detId in self.unmappedDetectorController.detectors.keyIterator():
            if detId not in unmappedDetectors.asDictionary():
                self.log("System check: Detector %s should not have been in unmapped Detectors" % detId,True)
                self.unmappedDetectorController.removeDetector(detId)

        for p in self.partitions:
            self.checkPartition(p)

        detectors = self.database.getAllDetectors()
        for d in detectors:
            self.checkDetector(d)

    def log(self,message,error=False,origin="ecs"):
        """log to file, terminal and Websocket"""
        str=datetime.now().strftime("%Y-%m-%d %H:%M:%S")+":" + message
        try:
            self.socketLogPublish.send(str.encode())
        except:
            self.socketLogPublish.close()
        if error:
            logging.critical(message)
        else:
            logging.info(message)
        """spread log message through websocket"""
        message = origin + ": "+str
        self.logQueue.append(message)
        self.webSocket.sendLogUpdate(message,"ecs")

    def terminateECS(self):
        """cleanup on shutdown"""
        for p in self.partitions:
            self.stopClient(p)
        self.terminate = True
        #to make get stop Blocking
        self.disconnectedPCAQueue.put(False)
        self.socketLogPublish.close()
        self.unmappedDetectorController.terminateContoller()
        self.zmqContext.term()
        self.zmqContextNoTimeout.term()

class PCAHandler:
    """Handler Object for Partition Agents"""
    def __init__(self,partitionInfo,ecsLogfunction,globalSystems,webSocket):
        #settings
        self.id = partitionInfo.id
        self.address = partitionInfo.address
        self.portLog = partitionInfo.portLog
        self.portCommand = partitionInfo.portCommand
        self.portPublish = partitionInfo.portPublish
        self.portCurrentState = partitionInfo.portCurrentState
        self.ecsLogfunction = ecsLogfunction
        self.globalSystems = globalSystems
        self.webSocket = webSocket

        self.context = zmq.Context()
        self.stateMap = ECS_tools.MapWrapper()
        self.logQueue = deque(maxlen=settings.BUFFERED_LOG_ENTRIES)

        self.PCAConnection = False
        self.receive_timeout = settings.TIMEOUT
        self.pingTimeout = settings.PINGTIMEOUT
        self.pingInterval = settings.PINGINTERVAL

        self.commandSocketAddress = "tcp://%s:%s" % (self.address,self.portCommand)

        #state Change subscription
        self.socketSubscription = self.context.socket(zmq.SUB)
        self.socketSubscription.connect("tcp://%s:%s" % (self.address, self.portPublish))
        #subscribe to everything
        self.socketSubscription.setsockopt(zmq.SUBSCRIBE, b'')

        #logsubscription
        self.socketSubLog = self.context.socket(zmq.SUB)
        self.socketSubLog.connect("tcp://%s:%s" % (self.address,self.portLog))
        self.socketSubLog.setsockopt(zmq.SUBSCRIBE, b'')

        t = threading.Thread(name="updater", target=self.waitForUpdates)
        r = ECS_tools.getStateSnapshot(self.stateMap,partitionInfo.address,partitionInfo.portCurrentState,timeout=self.receive_timeout,pcaid=self.id)
        if r:
            self.PCAConnection = True
        t.start()

        t = threading.Thread(name="logUpdater", target=self.waitForLogUpdates)
        t.start()
        t = threading.Thread(name="heartbeat", target=self.pingHandler)
        t.start()

    def createCommandSocket(self):
        """creates and returns a command socket"""
        socket = self.context.socket(zmq.REQ)
        socket.connect(self.commandSocketAddress)
        socket.setsockopt(zmq.RCVTIMEO, self.receive_timeout)
        socket.setsockopt(zmq.LINGER,0)
        return socket

    def pingHandler(self):
        """send heartbeat/ping"""
        socket = self.createCommandSocket()
        while True:
            try:
                socket.send(codes.ping)
                r = socket.recv()
                if not self.PCAConnection:
                    r = ECS_tools.getStateSnapshot(self.stateMap,self.address,self.portCurrentState,timeout=self.receive_timeout,pcaid=self.id)
                    if r:
                        self.log("PCA %s connected" % self.id)
                        self.PCAConnection = True
                    else:
                        self.handleDisconnection()
            except zmq.error.ContextTerminated:
                break
            except zmq.Again:
                self.handleDisconnection()
                #reset Socket
                socket.close()
                socket = self.createCommandSocket()
            except Exception as e:
                self.log("Exception while sending Ping: %s" % str(e))
                socket.close()
                socket = self.createCommandSocket()
            time.sleep(self.pingInterval)

    def handleDisconnection(self):
        """handler function for a pca disconnection"""
        if self.PCAConnection:
            self.log("PCA %s Connection Lost" % self.id)
        self.PCAConnection = False

    def sendCommand(self,command,arg=None):
        """send command to pca return True on Success"""
        command = [command]
        if arg:
            command.append(arg.encode())
        commandSocket = self.createCommandSocket()
        commandSocket.send_multipart(command)
        try:
            r = commandSocket.recv()
        except zmq.Again:
            self.log("timeout for sending command to PCA %s" % self.id)
            return False
        finally:
            commandSocket.close()
        if r != codes.ok:
            self.log("received error for sending command")
            return False
        return True


    def waitForUpdates(self):
        """wait for updates on subscription socket"""
        while True:
            try:
                m = self.socketSubscription.recv_multipart()
            except zmq.error.ContextTerminated:
                self.socketSubscription.close()
                break
            if len(m) != 3:
                self.log("received malformed update: %s" % str(m))
                continue
            else:
                id,sequence,state = m


            id = id.decode()
            sequence = ECS_tools.intFromBytes(sequence)
            if state == codes.reset:
                self.stateMap.reset()
                #reset code for Web Browser
                state = "reset"
            elif state == codes.removed:
                del self.stateMap[id]
                #remove code for Web Browser
                state = "remove"
            else:
                state = json.loads(state.decode())
                self.stateMap[id] = (sequence, stateObject(state))

            isGlobalSystem = id in self.globalSystems
            #send update to WebUI(s)
            jsonWebUpdate = {"id" : id,
                             "state" : state,
                             "sequenceNumber" : sequence,
                             "isGlobalSystem" : isGlobalSystem,
                            }
            if id == self.id and isinstance(state,dict):
                jsonWebUpdate["buttons"] = PCAStates.UIButtonsForState(state["state"])
            jsonWebUpdate = json.dumps(jsonWebUpdate)
            self.webSocket.sendUpdate(jsonWebUpdate,self.id)

    def log(self,message):
        """spread log message through websocket"""
        self.logQueue.append(message)
        self.ecsLogfunction(message,origin=self.id)
        self.webSocket.sendLogUpdate(message,self.id)

    def waitForLogUpdates(self):
        """wait for new log messages from PCA"""
        while True:
            try:
                m = self.socketSubLog.recv().decode()
            except zmq.error.ContextTerminated:
                self.socketSubLog.close()
                break
            self.log(m)

    def terminatePCAHandler(self):
        """cleanup on shutdown"""
        self.context.term()
